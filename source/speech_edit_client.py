"""Resumable client for DMSpeaks contextual-edit adapters.

An adapter can be local or hosted. It must implement the advertised edit
protocol; a conventional TTS endpoint is never assumed to support infilling.
The injected JSON transport owns authentication and HTTP timeouts.
"""
from __future__ import annotations
import copy,hashlib,json,re,time,uuid
from pathlib import Path
from urllib.parse import urlsplit
from speech_edit import plan_edits

class FailedSpeechEdit(ValueError):
    """The provider explicitly reported a terminal failed job."""

class PendingSpeechEdit(TimeoutError):
    def __init__(self,request_id):
        self.request_id=request_id
        super().__init__('Speech edit '+request_id+' is still pending. Resume this saved batch; do not create a duplicate request.')

class SpeechEditClient:
    def __init__(self,endpoint,request_json,poll_seconds=2):
        parts=urlsplit(endpoint)
        if parts.scheme not in ('http','https') or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:raise ValueError('Use a plain HTTP(S) service endpoint; keep credentials in the connection profile.')
        self.endpoint=endpoint.rstrip('/');self.request_json=request_json;self.poll_seconds=poll_seconds
    def capabilities(self):
        response=self.request_json(self.endpoint+'/capabilities')
        if response.get('schema')!='dmspeaks.provider_capabilities.0.1':raise ValueError('Service does not expose a compatible contextual-edit capability document.')
        caps=response.get('operations',{}).get('replace_spans',{})
        if caps.get('contextual_infilling') is not True:raise ValueError('Service does not support native contextual infilling. No ordinary TTS fallback was selected.')
        return copy.deepcopy(caps)
    def submit(self,request,slot_id,value_id,request_id,seed=42,max_new_frames=128):
        if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',request_id):raise ValueError('Invalid request_id.')
        jobs=plan_edits(request,self.capabilities())
        if not any(j['slot_id']==slot_id and j['value_id']==value_id for j in jobs):raise ValueError('Unknown replacement slot or candidate.')
        payload={'request_id':request_id,'edit_request':copy.deepcopy(request),'slot_id':slot_id,'value_id':value_id,'seed':seed,'max_new_frames':max_new_frames}
        return self.request_json(self.endpoint+'/infill/jobs',payload)
    def observe(self,request_id):
        if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',request_id):raise ValueError('Invalid request_id.')
        return self.request_json(self.endpoint+'/infill/jobs/'+request_id)
    def wait(self,job,timeout=1800,on_status=None):
        deadline=time.monotonic()+timeout
        while job.get('status') not in ('complete','failed'):
            if time.monotonic()>=deadline:raise PendingSpeechEdit(job['request_id'])
            if on_status:on_status(job['request_id'],job.get('status','unknown'))
            if self.poll_seconds:time.sleep(min(self.poll_seconds,max(0,deadline-time.monotonic())))
            job=self.observe(job['request_id'])
        if job['status']=='failed':raise FailedSpeechEdit('Contextual speech edit failed: '+job.get('error','unknown error'))
        return copy.deepcopy(job)
    def batch(self,request,state_path,seed=42,max_new_frames=128,timeout_per_job=1800,on_status=None):
        """Serialized, durable array expansion. A timeout preserves the same job ID."""
        jobs=plan_edits(request,self.capabilities());path=Path(state_path)
        signature=hashlib.sha256(json.dumps({'request':request,'endpoint':self.endpoint,'seed':seed,'max_new_frames':max_new_frames},sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        if path.exists():
            state=json.loads(path.read_text())
            if state.get('signature')!=signature:raise ValueError('Saved batch belongs to different inputs or service. Use a new state file.')
        else:
            state={'schema':'dmspeaks.speech_edit_batch.0.1','signature':signature,'endpoint':self.endpoint,'jobs':[{'request_id':'edit-'+uuid.uuid4().hex,'slot_id':j['slot_id'],'value_id':j['value_id'],'status':'not_submitted'} for j in jobs]}
        def persist():
            path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_name(path.name+'.next');temporary.write_text(json.dumps(state,indent=2));temporary.replace(path)
        persist()
        for item in state['jobs']:
            if item['status']=='complete':continue
            if item['status']=='failed':continue
            if item['status']=='not_submitted':
                # Intent is already persisted. Retrying a lost POST response uses
                # the same ID and the service verifies the complete payload.
                job=self.submit(request,item['slot_id'],item['value_id'],item['request_id'],seed,max_new_frames)
            else:job=self.observe(item['request_id'])
            item['status']=job['status'];persist()
            try:result=self.wait(job,timeout_per_job,on_status)
            except PendingSpeechEdit:raise
            except FailedSpeechEdit as error:
                item.update(status='failed',error=str(error));persist();continue
            item.update(status='complete',result=result);persist()
        return copy.deepcopy(state)
