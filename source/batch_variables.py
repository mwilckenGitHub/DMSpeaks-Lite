"""Plan finite variable clips and render only selected, still-missing pieces in context."""
import copy,uuid
from collections import defaultdict
from performance_plan import parse_record
from batch_speaker import batch_speaker_request
from variable_coverage import coverage_cases

def excluded_names(record):
 return {'name'}|{key.split('.')[1] for key,value in record.get('all_metadata',{}).items() if key.startswith('variables.') and key.endswith('.semantic_role') and value=='person_name'}

def record_domains(record,explicit,infer):
 domains={};defaults={};excluded=excluded_names(record)
 if infer:
  fields=record.get('all_metadata',{})
  for path,value in fields.items():
   parts=path.split('.')
   if len(parts)!=3 or parts[0]!='variables' or parts[1] in excluded:continue
   if parts[2]=='enum':domains[parts[1]]=value
   elif parts[2]=='type' and value=='boolean':domains.setdefault(parts[1],[True,False])
   elif parts[2]=='default':defaults[parts[1]]=value
 domains.update(explicit)
 return domains,defaults

def plan_variables(app,data):
 domains=data.get('domains',{});player=data.get('player',{})
 # Validate even if no imported row uses the requested domains.
 coverage_cases('Validation',player,domains)
 infer=data.get('use_corpus_enums',False)
 if type(infer) is not bool:raise ValueError('Use corpus enums must be true or false.')
 if not domains and not infer:raise ValueError('Choose finite variable values to cover. Name generation is a separate workflow.')
 source=app.store.load(data['dataset']);scope=data.get('scope','current')
 if scope not in ('current','all'):raise ValueError('Choose current dialogue or entire corpus.')
 index=data.get('index',0)
 if type(index) is not int or not 0<=index<len(source['rows']):raise ValueError('Choose a valid dialogue index.')
 indices=range(len(source['rows'])) if scope=='all' else [index]
 references=app.batch_references(data);groups=defaultdict(list)
 for asset in app.store.assets(newest_first=True):
  if asset.get('kind')=='piece' and asset.get('dataset') in (None,'',data['dataset']):groups[str(asset.get('line_id'))].append(asset)
 items=[];skipped=[];covered=0;unused=0
 for index in indices:
  record=app.store.record(data['dataset'],index)
  try:
   excluded=excluded_names(record)
   effective,defaults=record_domains(record,domains,infer)
   if excluded.intersection(effective):raise ValueError('Person-name variables belong in Missing name clips, not finite coverage.')
   context={**defaults,**player}
   for key,values in effective.items():
    if context.get(key) in (None,'') and isinstance(values,list) and values:context[key]=values[0]
   parsed=parse_record(record,context,data)
   used={s['type'] for s in parsed['segments'] if s['type']!='fixed'};effective={key:values for key,values in effective.items() if key in used}
   if not effective:unused+=1;continue
   request=batch_speaker_request(record,{**data,'index':index},references)
   generation=app.generation_key(record,request)
   cases=coverage_cases(record['text'],context,effective,excluded,parser=lambda text,values:parse_record({**record,'text':text},values,data))
   for case in cases:
    segments=parse_record(record,case['player'],request)['segments']
    compatible=app.audio.compatible(record,segments,request.get('variant','default'),request.get('reference_id',''),generation,assets=groups[str(record['id'])])
    missing=[s['order'] for s in compatible if s['order'] in case['target_orders'] and not s['options']]
    if not missing:covered+=1;continue
    items.append({'item_id':len(items),'index':index,'id':record['id'],'speaker':record['metadata'].get('npc_name',''),'player':case['player'],'variables':case['variables'],'missing_orders':missing,'revision':record['revision'],'metadata_revision':record['metadata_revision'],'generation_key':generation,'preview':parse_record(record,case['player'],request)['resolved_text'][:220]})
  except ValueError as error:skipped.append({'index':index,'id':record['id'],'reason':str(error)})
 ident=uuid.uuid4().hex;app.plans[ident]={'kind':'variable_coverage','data':copy.deepcopy(data),'items':copy.deepcopy(items)}
 return {'plan_id':ident,'items':items,'skipped':skipped,'generation_count':len(items),'needed_clip_count':sum(len(x['missing_orders']) for x in items),'covered_cases':covered,'unused_dialogues':unused,'notice':'Each selected case renders the full contextual passage, retaining only its missing fixed or finite-variable clips. Enum values are covered independently, not as a Cartesian product. Name clips are not created. Cloud billing may include every full passage.'}

def run_variables(app,data,status):
 plan=app.plans.get(data.get('plan_id'))
 if not plan or plan.get('kind')!='variable_coverage':raise ValueError('This coverage plan expired. Scan again.')
 selected=data.get('item_ids')
 if not isinstance(selected,list) or not selected or any(type(i) is not int for i in selected):raise ValueError('Choose one or more coverage cases.')
 selected=set(selected);valid={item['item_id'] for item in plan['items']}
 if not selected.issubset(valid):raise ValueError('A selected case is absent from this coverage plan.')
 done=[];failed=[];references=app.batch_references(plan['data'])
 for item in plan['items']:
  if item['item_id'] not in selected:continue
  status('Variable clips: '+str(len(done)+len(failed)+1)+'/'+str(len(selected))+' — '+str(item['speaker'] or item['id']))
  request={**plan['data'],'index':item['index'],'player':item['player'],'generation_mode':'context','source_revision':item['revision'],'source_metadata_revision':item['metadata_revision']}
  try:
   record=app.record(request);request=batch_speaker_request(record,request,references)
   if record['revision']!=item['revision'] or record['metadata_revision']!=item['metadata_revision'] or app.generation_key(record,request)!=item['generation_key']:raise ValueError('Source, speaker or speech settings changed. Scan again.')
   segments=parse_record(record,request['player'],request)['segments']
   compatible=app.audio.compatible(record,segments,request.get('variant','default'),request.get('reference_id',''),item['generation_key'])
   missing=[s['order'] for s in compatible if s['order'] in item['missing_orders'] and not s['options']]
   if not missing:done.append({'item_id':item['item_id'],'index':item['index'],'reused':True});continue
   result=app._generate({**request,'only_orders':missing},status)
   if result['warning']:raise ValueError(result['warning'])
   if set(missing)!={p['slot_order'] for p in result['pieces']}:raise ValueError('Some requested clip boundaries were unavailable. Full take is retained for review.')
   done.append({'item_id':item['item_id'],'index':item['index'],'player':item['player'],'piece_ids':[p['id'] for p in result['pieces']]})
  except Exception as error:failed.append({'item_id':item['item_id'],'index':item['index'],'error':str(error)})
 return {'completed':done,'failed':failed}
