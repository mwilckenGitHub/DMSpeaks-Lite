"""Explicit local/web reference discovery with owned caches and pre-render checks."""
import hashlib,json,re,shutil,urllib.parse,urllib.request,uuid
from pathlib import Path
from audio import probe

def file_hash(path):
 h=hashlib.sha256()
 with Path(path).open('rb') as f:
  for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
 return h.hexdigest()
def sync_catalog(app):
 root=app.folder/'references';root.mkdir(exist_ok=True);refs=app.store.setting('references',[]) or [];items=[]
 for ref in refs:
  path=Path(ref.get('path',''));available=path.is_file();items.append({'id':ref['id'],'name':ref.get('name',ref['id']),'uri':ref.get('source_locator',{}).get('uri') or str(path),'cached_path':str(path),'transcript':ref.get('transcript',''),'source_metadata':ref.get('source_metadata',{}),'audio_metadata':ref.get('audio_metadata',{}),'available':available,'audio_sha256':file_hash(path) if available else None})
 target=root/'index.json';tmp=root/('index-'+uuid.uuid4().hex+'.tmp');tmp.write_text(json.dumps({'format':'voxweave-reference-library','version':'0.1','references':items},ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(target);return target

def load_source(app,uri):
 if not isinstance(uri,str) or not uri.strip():raise ValueError('Choose an audio file path or direct HTTPS audio URL.')
 uri=uri.strip()
 if uri.startswith('https://'):asset=app.fetch_reference(uri)
 else:
  if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://',uri) and not uri.startswith('file://'):raise ValueError('Use a local file path, file URI or direct HTTPS audio URL.')
  parsed=urllib.parse.urlsplit(uri)
  if uri.startswith('file://') and parsed.netloc not in ('','localhost'):raise ValueError('Use a local file URI or direct HTTPS audio URL.')
  path=Path(urllib.request.url2pathname(parsed.path) if uri.startswith('file://') else uri).expanduser()
  if not path.is_file():raise ValueError('The sample path is not accessible on the companion computer. Upload the file if it is on another computer.')
  with path.open('rb') as f:content=f.read(30_000_001)
  asset=app.import_reference(content,path.name)
 asset['source_locator']={'uri':uri,'kind':'https' if uri.startswith('https://') else 'local'};app.store.add_asset(asset);return asset

def discover(app,data,status=lambda _:None):
 folder=Path(data.get('folder') or app.lab/'profiles').expanduser()
 if not folder.is_dir():raise ValueError('Choose an accessible folder on the companion computer.')
 refs=app.store.setting('references',[]) or [];known={r['id']:r for r in refs};added=[];issues=[];candidates=[]
 for file in sorted(folder.glob('*.json')):
  if file.name.startswith('studio-'):continue
  try:
   value=json.loads(file.read_text(encoding='utf-8'))
   if isinstance(value,dict) and isinstance(value.get('references'),list):
    for item in value['references']:candidates.append((folder,item))
   elif isinstance(value,dict) and any(k in value for k in ['reference_file','uri']):candidates.append((folder,{**value,'id':value.get('id',file.stem)}))
  except (ValueError,OSError) as exc:issues.append({'file':file.name,'error':str(exc)})
 if not candidates:
  for file in sorted(folder.iterdir()):
   if file.suffix.lower() in {'.wav','.ogg','.mp3','.flac','.m4a'}:
    transcript=file.with_suffix('.txt');candidates.append((folder,{'id':file.stem,'uri':str(file),'transcript':transcript.read_text(encoding='utf-8') if transcript.is_file() else ''}))
 if len(candidates)>1000:raise ValueError('Import at most 1,000 reference descriptors in one scan.')
 for n,(base,item) in enumerate(candidates):
  ident=item.get('id');status(f'Checking references: {n+1}/{len(candidates)}')
  try:
   if not isinstance(ident,str) or not re.fullmatch(r'[\w-]{1,100}',ident):raise ValueError('Use a simple unique reference ID.')
   if ident in known:continue  # Existing bindings and user selections are never silently replaced.
   uri=item.get('uri') or item.get('reference_file');text=item.get('transcript',item.get('reference_text',''))
   if not isinstance(text,str) or not text.strip():raise ValueError('An exact transcript is required; add a matching .txt sidecar or descriptor transcript.')
   if not isinstance(uri,str):raise ValueError('A local or HTTPS sample URI is required.')
   if not uri.startswith(('https://','file://')) and not Path(uri).is_absolute():uri=str(base/uri)
   asset=load_source(app,uri);ref={'id':ident,'name':str(item.get('name') or ident),'asset_id':asset['id'],'path':asset['path'],'transcript':text,'source_locator':asset['source_locator'],'fish_profile':ident};refs.append(ref);known[ident]=ref;added.append(ident)
  except (ValueError,OSError) as exc:issues.append({'id':ident,'error':str(exc)})
 app.store.set_setting('references',refs);sync_catalog(app);return {'added':added,'issues':issues,'references':refs}

def native_reference(lab,voice,p):
 if not voice or Path(voice).name!=voice:raise ValueError('Choose a reference sample or voice ID before rendering.')
 config=Path(p.get('profiles_dir') or Path(lab)/'profiles')/(voice+'.json')
 if not config.is_file():raise ValueError(f'No reference is configured for {voice}. Choose an available Reference sample, import your own, or configure a voice ID.')
 raw=json.loads(config.read_text(encoding='utf-8'));path=Path(raw.get('reference_file',''));text=raw.get('reference_text',raw.get('transcript',''))
 if not path.is_file():raise ValueError(f'The local sample for {voice} is unavailable. Load its local path or HTTPS source in Reference first.')
 if not str(text).strip():raise ValueError(f'The reference for {voice} needs an exact transcript.')
 return {'id':voice,'name':voice,'path':str(path),'transcript':text,'fish_profile':voice}

def corpus_reference(app,record,voice):
 """Resolve a corpus-owned local sample without fetching or changing settings."""
 from audio_sources import embedded
 if not record.get('dataset'):return None
 sources=embedded(app.store.load(record['dataset'])).get('sources',[])
 matches=[source for source in sources if source.get('id')==voice or voice in source.get('voice_profile_refs',[])]
 if not matches:return None
 if len(matches)>1:raise ValueError(f'Multiple corpus audio sources match {voice}. Choose an explicit Reference override.')
 declaration=matches[0];uri=declaration.get('uri','').strip()
 from portable_references import local_path
 path=local_path(app,record['dataset'],declaration)
 if not uri:raise ValueError(f'Corpus reference {voice} has no audio path.')
 if path is None and uri.startswith('https://'):
  raise ValueError(f'Corpus reference {voice} uses an online sample. Fetch/import it in Reference and select the cached sample before rendering.')
 if path is None and '://' in uri:
  parsed=urllib.parse.urlsplit(uri)
  if parsed.scheme!='file' or parsed.netloc not in ('','localhost'):raise ValueError('Use a local audio path, local file URI, or import a direct HTTPS sample.')
 if path is None:raise ValueError(f'Corpus reference {voice} is not accessible on the companion computer. Import the sample or update its corpus path.')
 transcript=declaration.get('transcript','')
 if not isinstance(transcript,str) or not transcript.strip():raise ValueError(f'Corpus reference {voice} needs its exact transcript before rendering.')
 return {'id':declaration['id'],'name':declaration.get('name') or declaration['id'],'path':str(path),'transcript':transcript,'fish_profile':declaration['id'],'source_metadata':declaration.get('source_metadata',{}),'source_locator':{'uri':uri,'kind':'local'},'reference_origin':'corpus'}

def resolve(app,record,data):
 refs=app.store.setting('references',[]) or [];ident=data.get('reference_id')
 if ident:
  ref=next((r for r in refs if r['id']==ident),None)
  if ref is None:raise ValueError('The selected reference is not registered. Select or import a reference sample.')
 else:
  p=app.connections.profile(data['provider'])
  if p['engine']=='fish':
   # A hosted provider voice can be an explicit override. Otherwise honour the
   # corpus assignment; never search unrelated machine-local voice profiles
   # and upload them to a cloud service implicitly.
   profile=record['metadata'].get('npc_voice_profile','')
   if data.get('voice') or p.get('voice.'+str(profile)) or 'references' not in p.get('capabilities','').split(','):return None
   ref=corpus_reference(app,record,profile) if profile else None
   if ref is None:return None
  else:
   if p['engine'] not in {'fish_resident','fish_omni'}:return None
   voice=app.providers.resolve_voice(data['provider'],record['metadata'].get('npc_voice_profile',''),data.get('voice',''))
   if not voice or Path(voice).name!=voice:raise ValueError('Choose a reference sample or configured voice ID before rendering.')
   ref=corpus_reference(app,record,voice) or native_reference(app.lab,voice,p)
 path=Path(ref.get('path',''))
 if not path.is_file():raise ValueError(f'Reference {ref["id"]} is unavailable. Re-import its local file or HTTPS source.')
 if not str(ref.get('transcript','')).strip():raise ValueError(f'Reference {ref["id"]} needs its exact transcript before rendering.')
 return ref

def default_status(app,data):
 """Describe the exact implicit render reference without importing or fetching it."""
 record=app.record(data)
 try:
  ref=resolve(app,record,{'provider':data['provider'],'voice':data.get('voice','')})
 except (ValueError,OSError) as exc:return {'status':'missing','reason':str(exc)}
 if ref is None:return {'status':'not_applicable','reason':'This connection does not resolve a local default reference. Choose a supported sample or provider voice ID.'}
 return {'status':'ready','reference':{k:ref[k] for k in ('id','name','path','transcript') if k in ref},'origin':ref.get('reference_origin','local_profile')}

def audit(app,data,status=lambda _:None):
 from batch_speaker import batch_speaker_request
 dataset=data['dataset'];source=app.store.load(dataset);groups={};refs=app.store.setting('references',[]) or []
 p=app.connections.profile(data['provider']);caps={x.strip() for x in p.get('capabilities','').split(',')};result=[]
 for i in range(len(source['rows'])):
  record=app.store.record(dataset,i);profile=str(record['metadata'].get('npc_voice_profile') or record.get('speaker',{}).get('voice_profile_ref') or 'unassigned')
  request=batch_speaker_request(record,{**data,'voice_bindings':data.get('voice_bindings',{})},refs if 'references' in caps else [])
  key=(profile,request.get('reference_id',''),request.get('voice',''))
  if key not in groups:groups[key]={'profile':profile,'dialogues':0,'record':record,'request':request}
  groups[key]['dialogues']+=1
 for group in groups.values():
  profile=group['profile'];request=group['request'];row={k:v for k,v in group.items() if k not in {'record','request'}}
  try:
   if 'references' in caps:
    chosen=resolve(app,group['record'],request)
    if chosen:info=probe(chosen['path']);row.update(status='ready',reference_id=chosen['id'],seconds=info['seconds'],audio_sha256=file_hash(chosen['path']))
    else:row.update(status='missing',reason='Choose an explicit reference for this service.')
   else:
    voice=app.providers.resolve_voice(data['provider'],profile,request.get('voice',''));row.update(status='configured-unverified' if voice else 'missing',voice_id=voice,reason='Provider voice mapping/default is configured; remote synthesis has not been verified.' if voice else 'Set a provider voice ID.')
  except (ValueError,OSError) as exc:row.update(status='missing',reason=str(exc))
  result.append(row);status('Checking voice profiles: '+str(len(result)))
 return {'profiles':result,'ready':sum(r['status']=='ready' for r in result),'missing':sum(r['status']=='missing' for r in result),'remote_unverified':sum(r['status']=='configured-unverified' for r in result)}
