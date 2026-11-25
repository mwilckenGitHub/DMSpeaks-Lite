"""Provider execution is independent of corpus/UI. Fish is one optional adapter."""
from __future__ import annotations
import json,os,re,subprocess,time,urllib.error,urllib.parse,urllib.request,uuid
from pathlib import Path

class _NoCheckRedirects(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):return None


def _open_connection_check(request, timeout):
 # A redirect can forward credentials to a destination the user did not select.
 return urllib.request.build_opener(_NoCheckRedirects()).open(request, timeout=timeout)


class Providers:
 def __init__(self,connections,output,lab):self.connections=connections;self.output=Path(output);self.lab=Path(lab);self.output.mkdir(parents=True,exist_ok=True)
 def _request(self,url,payload=None,headers=None,timeout=30):
  req=urllib.request.Request(url,data=None if payload is None else json.dumps(payload).encode(),headers={'Content-Type':'application/json',**(headers or {})})
  try:
   with urllib.request.urlopen(req,timeout=timeout) as r:return json.load(r)
  except urllib.error.HTTPError as e:raise ValueError(f'TTS endpoint returned HTTP {e.code}. Check endpoint, model, voice and credentials.') from None
 def check(self,ident,*,profile=None):
  result=self._check(ident,profile)
  return {**result,'speech_generated':False,'voice_identity_verified':False,'contextual_infilling_verified':False,
          'message':result['message']+' Generated speech, voice matching and native word replacement were not tested.'}
 def _check(self,ident,profile=None):
  p=profile if profile is not None else self.connections.profile(ident);engine=p['engine']
  if engine=='qwen_cli':
   launcher=Path(p.get('launcher',str(self.lab/'voice-lab'))).expanduser()
   if not launcher.is_file():raise ValueError('No Qwen runtime launcher is configured. Set launcher in the INI or use an HTTP connection.')
   return {'ok':True,'message':'Local Qwen launcher available. Model inference has not been run by this connection check.'}
  if engine=='polly':
   client=self._polly(p);voices=client.describe_voices().get('Voices',[]);return {'ok':True,'message':f'AWS connection verified; {len(voices)} voices returned.'}
  endpoint=self.connections.endpoint(p);secret=self.connections.secret(ident,p)
  if engine=='fish_omni':
   from fish_community_tts import check
   if not check(endpoint):raise ValueError('Community Fish is unavailable. Start the configured vLLM-Omni runtime; the legacy worker was not used.')
   return {'ok':True,'message':'Community Fish vLLM-Omni is healthy. No speech was requested.'}
  if engine=='fish_resident':
   health=self._request(endpoint+'/health');return {'ok':health.get('state') in ('ready','rendering'),'message':'Resident Fish: '+health.get('state','unknown'),'model_instance_id':health.get('model_instance_id'),'model_load_count':health.get('model_load_count')}
  if p.get('transport')=='cloud' and not secret:raise ValueError('Add the connection credential or set its configured environment variable first.')
  path=p.get('check_path') or {'elevenlabs':'/v1/voices','openai':'/models' if endpoint.endswith('/v1') else '/v1/models','azure':'/cognitiveservices/voices/list','google':'/v1/voices','fish':'/model'}.get(engine,'/health')
  headers={}
  if engine=='elevenlabs':headers['xi-api-key']=secret
  elif engine=='azure':headers['Ocp-Apim-Subscription-Key']=secret
  elif secret:headers['Authorization']='Bearer '+secret
  if not re.fullmatch(r'/(?!/)[^\s?#]*', path):
   raise ValueError('Enter a relative health-check path beginning with one slash.')
  if engine=='google':
   if profile is None:path+='?key='+urllib.parse.quote(secret,safe='')
   elif p.get('quota_project'):headers['x-goog-user-project']=p['quota_project']
  req=urllib.request.Request(endpoint+path,headers=headers)
  try:
   with _open_connection_check(req,timeout=20) as response:response.read(2000)
  except urllib.error.HTTPError as e:
   if 300<=e.code<400:raise ValueError('The service redirected the check. Use its final service URL.') from None
   raise ValueError(f'Connection check returned HTTP {e.code}. Check credentials and the health-check path in Advanced settings.') from None
  except (urllib.error.URLError,TimeoutError,OSError):raise ValueError('Could not reach this service. Check its URL and whether it is running.') from None
  return {'ok':True,'message':'Connection responded successfully. No speech was requested by this check.'}
 def _polly(self,p):
  try:import boto3
  except ImportError:raise ValueError('Amazon Polly needs the optional boto3 package installed in the backend environment.') from None
  return boto3.Session(profile_name=p.get('aws_profile') or None,region_name=p.get('region','us-east-1')).client('polly')
 def resolve_voice(self,ident,speaker_profile='',override=''):
  p=self.connections.profile(ident)
  if speaker_profile is None:speaker_profile=''
  return override or p.get('voice.'+speaker_profile) or (speaker_profile if p['engine'] in {'fish_resident','fish_omni'} else p.get('voice',''))
 def generate(self,ident,text,voice,reference=None,seed=42,rate=1.0,on_status=None):
  p=self.connections.profile(ident);engine=p['engine'];voice=p.get('voice.'+voice) or voice or p.get('voice','');jobid='studio-'+uuid.uuid4().hex
  original_text=text;leading=[]
  while (match:=re.match(r'^\s*\[([^\]]*)\]\s*',text)):
   leading.append(match[1]);text=text[match.end():]
  if p.get('syntax')!='brackets' and re.search(r'\[[^\]]+\]',text):raise ValueError('Inline bracket directions are unsupported by this adapter. Use a leading global direction, supported SSML, or a bracket-capable engine.')
  instructions=', '.join(leading);path=self.output/(jobid+'.wav');metadata={'engine':engine,'provider':ident,'voice':voice,'model':p.get('model'),'seed':seed,'rate':float(rate),'instructions':instructions}
  if engine=='fish_omni':
   from fish_community_tts import generate
   if reference is None:
    from reference_catalog import native_reference
    reference=native_reference(self.lab,voice,p)
   if float(rate)!=1:raise ValueError('This community adapter does not declare a numeric speed control. Use supported text directions for a new speaking pace.')
   if on_status:on_status('Community Fish synthesis')
   budget=int(p['max_new_tokens']) if p.get('max_new_tokens') else None
   output,words,extra=generate(self.connections.endpoint(p),original_text,reference,path,model=p.get('model','drama-3'),seed=int(seed),timeout=int(p.get('timeout',1800)),max_new_tokens=budget)
   return output,words,{**metadata,**extra,'runtime':'community-vllm-omni'}
  if engine=='fish_resident':
   if p.get('transport')=='ssh':raise ValueError('Use the Fish HTTP adapter for SSH; resident output paths are local only.')
   from reference_profile import resident_reference_profile
   if reference is None:
    from reference_catalog import native_reference
    reference=native_reference(self.lab,voice,p)
   endpoint=self.connections.endpoint(p);profile=resident_reference_profile(reference,self.lab) if reference is not None else voice
   metadata.update(voice_profile=profile,reference_id=(reference or {}).get('id'))
   payload={'request_id':jobid,'text':text,'direction':instructions,'voice_profile':profile,'seed':int(seed)}
   job=self._request(endpoint+'/jobs',payload);deadline=time.monotonic()+int(p.get('timeout',1800))
   while job['status'] not in ('complete','failed'):
    if time.monotonic()>deadline:raise ValueError('Fish is still processing the request; its resident worker has been left running. Check the job before retrying.')
    if on_status:on_status('Fish '+job['status'])
    time.sleep(2);job=self._request(endpoint+'/jobs/'+jobid)
   if job['status']=='failed':raise ValueError('Fish generation failed: '+job.get('error','unknown error'))
   import shutil
   # Only local worker responses may reference a local file. SSH workers need a conventional HTTP TTS adapter.
   if p.get('transport')=='ssh':raise ValueError('Use the Fish HTTP adapter over SSH; resident-worker local paths are not transferable.')
   shutil.copy2(job['output'],path);metadata.update({k:job[k] for k in ['model_instance_id','model_load_count','inference_seconds'] if k in job});return path,[],metadata
  if engine=='qwen_cli':
   launcher=Path(p.get('launcher',str(self.lab/'voice-lab'))).expanduser();model=p.get('model','qwen-base')
   if model not in {'qwen-base','qwen-custom','qwen-design'}:raise ValueError('Qwen CLI model must be qwen-base, qwen-custom or qwen-design.')
   if not launcher.is_file():raise ValueError('Configure a Qwen runtime launcher or select a local HTTP service.')
   from model import clean_spoken
   args=[str(launcher),model,'--text',clean_spoken(text),'--output',str(path)]
   # These controls are not part of the configured launcher contract. Keep the
   # request for provenance without claiming a seed or pace reached the runtime.
   metadata.update(requested_seed=seed,requested_rate=float(rate),requested_voice=voice,seed=None,rate=None,voice=None,control_warnings=[])
   if seed!=42:metadata['control_warnings'].append('The Qwen CLI adapter does not forward seed; the launcher uses its own default.')
   if float(rate)!=1.0:metadata['control_warnings'].append('The Qwen CLI adapter does not forward speech rate. Use playback or layer speed for a measured pace change.')
   if model=='qwen-base':
    if not reference or not reference.get('path') or not reference.get('transcript'):raise ValueError('Qwen voice cloning needs a selected reference set and its exact transcript.')
    if instructions:raise ValueError('This Qwen Base runtime does not accept emotion instructions. Choose CustomVoice/VoiceDesign or remove directions.')
    args+=['--reference',reference['path'],'--reference-text',reference['transcript']];metadata['reference_id']=reference.get('id')
   elif model=='qwen-custom':
    speaker=voice or 'Ryan';args+=['--direction',instructions,'--speaker',speaker];metadata['voice']=speaker
   else:args+=['--direction',instructions]
   if model!='qwen-custom' and voice:metadata['control_warnings'].append('This Qwen model does not use a speaker ID; its voice comes from the reference or design instructions.')
   if on_status:on_status('Qwen local inference')
   response=subprocess.run(args,capture_output=True,timeout=int(p.get('timeout',1800)),
       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
   if response.returncode:raise ValueError('Qwen runtime failed: '+response.stderr.decode(errors='replace')[-600:])
   return path,[],metadata
  if engine=='polly':
   from model import clean_spoken
   content=original_text if p.get('syntax')=='ssml' and original_text.lstrip().startswith('<speak') else clean_spoken(original_text)
   options={'Text':content,'TextType':'ssml' if content.lstrip().startswith('<speak') else 'text','VoiceId':voice or p.get('voice','Joanna'),'Engine':p.get('model','neural'),'OutputFormat':'mp3','SampleRate':'24000'}
   result=self._polly(p).synthesize_speech(**options);path=path.with_suffix('.mp3');path.write_bytes(result['AudioStream'].read());return path,[],metadata
  import http_protocols
  secret=self.connections.secret(ident,p)
  if p.get('transport')=='cloud' and not secret:raise ValueError('This cloud profile has no credential. Add one in Connections or set its INI environment variable.')
  p={**p,'endpoint':self.connections.endpoint(p),'seed':str(seed)}
  request=http_protocols.build_request(p,original_text,instructions,float(rate),voice,secret,reference=reference if engine=='fish' else None)
  if request.get('reference_evidence'):metadata['reference']=request['reference_evidence']
  req=urllib.request.Request(request['url'],data=request['body'],headers=request['headers'],method=request.get('method','POST'))
  try:
   with urllib.request.urlopen(req,timeout=int(p.get('timeout',180))) as response:data=response.read(100_000_001)
  except urllib.error.HTTPError as e:raise ValueError(f'{engine} returned HTTP {e.code}. Check voice, model, account balance and credentials.') from None
  if len(data)>100_000_000:raise ValueError('The provider returned an oversized response.')
  audio,words=http_protocols.parse_response(engine,data,timestamps='timestamps' in p.get('capabilities',''));path.write_bytes(audio);return path,words,metadata
