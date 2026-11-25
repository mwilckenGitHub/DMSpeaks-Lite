"""Offline complete dialogue files, independent of any game or TTS provider."""
import copy,csv,hashlib,json,math,re,sqlite3,tempfile
from pathlib import Path
from urllib.parse import quote
from audio import encode_pcm
from delivery_timing import measure_delivery
from library_export import file_hash,TIMING_KEYS
from library_manifest import UnavailableSequence
from library_paths import complete_path,COMPLETE_TEMPLATE,COMPLETE_FIELDS
from library_preview import render_exported_sequence
from model import clean_spoken
from performance_plan import parse_entry
from timing_summary import summarize_timing

IDENTITY_KEYS=['corpus_id','dialogue_index','dialogue_id','quest_id','npc_id','npc_name','npc_voice_profile','dialogue_stage','dialogue_source_revision','dialogue_metadata_revision']
COLUMNS=[*IDENTITY_KEYS,'source_metadata_json','profile_id','export_status','reason','audio_path','audio_metadata_path','audio_sha256',*TIMING_KEYS,'missing_orders_json','selection_context_json']
REPORT_PAGE_SIZE=20

def validate_options(options):
 if not isinstance(options,dict):raise ValueError('Complete dialogue options must be an object.')
 player=options.get('player',{});variant=options.get('variant','default')
 if not isinstance(player,dict) or any(not isinstance(k,str) or v is not None and not isinstance(v,(str,int,float,bool)) or isinstance(v,float) and not math.isfinite(v) for k,v in player.items()):raise ValueError('Test character values must be named text, number, boolean or null values.')
 if not isinstance(variant,str) or not variant:raise ValueError('Choose a voice variant for complete dialogues.')
 complete_path(options.get('path_template',COMPLETE_TEMPLATE),{key:0 if key=='dialogue_index' else 'example' for key in COMPLETE_FIELDS})
 profile={'values':copy.deepcopy(player),'variant':variant}
 canonical=json.dumps(profile,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
 profile['id']=hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]
 return profile

def export_complete_dialogues(root,index,options,silence_ms,status):
 """Stream one profile's finished files; advertise only files that were verified."""
 root=Path(root);profile=validate_options(options);player=profile['values'];available=unavailable=partial=0
 template=options.get('path_template',COMPLETE_TEMPLATE)
 summary={'index_path':'complete-dialogues.json','csv_path':'complete-dialogues.csv','profile':profile,'path_template':template}
 index.execute('''create table finished_report(
  position integer primary key,slot integer not null,status text not null,
  title text not null,speaker text not null,reason text not null,missing integer not null,
  seconds real,search_text text not null)''')
 index.execute('create index finished_report_status on finished_report(status,position)')
 catalog=None
 if (root/'manifest.jsonl').is_file():
  from library_index import CatalogIndex
  catalog=CatalogIndex(root/'manifest.jsonl',root.parent/'runtime-index',lambda progress:status('Indexing exported catalog…'))
 with (root/summary['index_path']).open('w',encoding='utf-8') as jf,(root/summary['csv_path']).open('w',encoding='utf-8-sig',newline='') as cf:
  writer=csv.DictWriter(cf,fieldnames=COLUMNS,lineterminator='\n');writer.writeheader()
  jf.write('{"format":"dmspeaks-complete-dialogues","version":"0.1","profile":'+json.dumps(profile,ensure_ascii=False)+',"path_template":'+json.dumps(template)+',"dialogues":[')
  position=-1;first=True
  while True:
   row=index.execute('select position,record,value from annotated where position>? order by position limit 1',(position,)).fetchone()
   if row is None:break
   position,slot,value=row
   entry=json.loads(value);item={k:entry.get(k) for k in IDENTITY_KEYS};item.update(profile_id=profile['id'],source_metadata=entry.get('source_metadata',{}))
   status('Assembling complete dialogue '+str(available+unavailable+1)+'…')
   try:
    pcm,arrangement,resolved,assets=render_exported_sequence(root,entry,player,variant=profile['variant'],silence_ms=silence_ms,status=status,indexed_catalog=catalog)
   except UnavailableSequence as exc:
    item.update(export_status='unavailable',reason=str(exc));unavailable+=1
   else:
    with tempfile.TemporaryDirectory(dir=root.parent,prefix='complete-') as work:
     encoded=Path(work)/'recording.ogg';encode_pcm(pcm,encoded,channels=arrangement['channels']);delivery=measure_delivery(encoded);digest=file_hash(encoded)
     relative=Path(complete_path(template,{**item,'audio_id':digest[:24]}));path=root/relative;metadata_path=relative.with_suffix('.json')
     for destination in [relative,metadata_path]:
      try:index.execute('insert into paths values(?,?)',(destination.as_posix().casefold(),'complete:'+str(slot)))
      except sqlite3.IntegrityError as exc:raise ValueError('Complete recordings resolve to an existing output path. Include {dialogue_index} in the template to distinguish repeated text.') from exc
     try:path.parent.mkdir(parents=True,exist_ok=True);encoded.replace(path)
     except (FileExistsError,NotADirectoryError) as exc:raise ValueError('The complete-recording template conflicts with another exported file or folder.') from exc
    if delivery['audio_sample_count']<arrangement['sample_count']:raise ValueError('The complete recording lost source frames during encoding.')
    timing=summarize_timing(list(assets.values()))
    parsed=parse_entry(entry,player)
    detail={'format':'dmspeaks-complete-dialogue','version':'0.1',**item,'profile':profile,'source_text':entry['active_source_text'],'resolved_text':parsed['resolved_text'],'spoken_text':parsed['spoken_text'],'selection_context':resolved['selection_context'],'missing_orders':resolved['missing_orders'],'source_pcm_sample_count':arrangement['sample_count'],'source_pcm_sample_rate':44100,'delivery':delivery,'slots':arrangement['slots'],'words':arrangement['words'],'master_gain':arrangement['master_gain'],**timing,'word_timing_notice':'Shifted selected clip timings, not newly aligned to this encoded recording. Missing variable clips have no word entries.','processing':'Sequential complete clips at original speed, with overload attenuation if needed and no additional edge fades. Current waveform edits are exported separately when requested.'}
    detail['requested_spoken_text']=detail['spoken_text']
    detail['spoken_text']=clean_spoken(''.join(slot.get('resolved_text','') for slot in arrangement['slots'] if slot['kind']=='audio'))
    (root/metadata_path).write_text(json.dumps(detail,ensure_ascii=False),encoding='utf-8')
    item.update(export_status='available',audio_path=relative.as_posix(),audio_metadata_path=metadata_path.as_posix(),audio_sha256=digest,missing_orders=resolved['missing_orders'],selection_context=resolved['selection_context'],**delivery);available+=1
   jf.write(('' if first else ',')+json.dumps(item,ensure_ascii=False));first=False
   writer.writerow({**{k:v for k,v in item.items() if k in COLUMNS},'source_metadata_json':json.dumps(item['source_metadata'],ensure_ascii=False),'missing_orders_json':json.dumps(item.get('missing_orders',[])),'selection_context_json':json.dumps(item.get('selection_context',{}))})
   entry['complete_dialogue']=item;index.execute('update annotated set value=? where record=?',(json.dumps(entry,ensure_ascii=False),slot))
   missing=len(item.get('missing_orders',[]))
   ready=item['export_status']=='available'
   report_status=('partial' if missing else 'ready') if ready else 'unavailable'
   partial+=report_status=='partial'
   title=str(entry.get('dialogue_title') or ' '.join(entry['active_source_text'].split()) or item['dialogue_id'])
   title=title if len(title)<=140 else title[:139]+'…'
   speaker=str(item.get('npc_name') or '')
   reason=item.get('reason','')
   if report_status=='partial':
    by_order={segment['order']:segment for segment in parsed['segments']}
    examples=[f"{by_order[order]['placeholder']} (segment {order})"
              for order in item['missing_orders'][:3]]
    reason='No replacement audio for '+', '.join(examples)+('…' if missing>3 else '.')
   search_text=' '.join([title,speaker,str(item['quest_id']),str(item['dialogue_id']),reason]).casefold()
   index.execute('insert into finished_report values(?,?,?,?,?,?,?,?,?)',
    (position,slot,report_status,title,speaker,reason,missing,item.get('audio_seconds'),search_text))
  jf.write('],"available":'+str(available)+',"unavailable":'+str(unavailable)+',"notice":"Offline authoring output. A target adapter must map dialogue identities, guard the character profile, and verify runtime playback."}')
 if catalog is not None:catalog.close()
 return {**summary,'available':available,'unavailable':unavailable,'with_missing_replacements':partial}


def finished_report(app,data):
 """Read one bounded page from a completed export's own visible-order index."""
 ident=data.get('export_id','')
 if not isinstance(ident,str) or not re.fullmatch('[a-f0-9]{32}',ident):
  raise ValueError('Choose a completed library.')
 jobdir=app.folder/'exports'/ident
 database=jobdir/'index.sqlite'
 if not database.is_file() or not (jobdir/'voxweave-audio-library.zip').is_file():
  raise ValueError('That completed library is not available.')
 filters={'all':(), 'attention':('partial','unavailable'),
          'ready':('ready',),'partial':('partial',),'unavailable':('unavailable',)}
 selection=data.get('filter','all')
 query=data.get('query','').strip().casefold()
 offset=str(data.get('offset',0))
 if selection not in filters or len(query)>200 or not re.fullmatch('[0-9]{1,8}',offset):
  raise ValueError('Choose a valid report filter, search or page.')
 clauses=[];parameters=[]
 statuses=filters[selection]
 if statuses:
  clauses.append('status in ('+','.join('?' for _ in statuses)+')')
  parameters.extend(statuses)
 if query:
  clauses.append('instr(search_text,?)>0');parameters.append(query)
 where=' where '+' and '.join(clauses) if clauses else ''
 with sqlite3.connect('file:'+quote(str(database.resolve()))+'?mode=ro',uri=True) as index:
  if not index.execute("select 1 from sqlite_master where name='finished_report'").fetchone():
   raise ValueError('Build a library with complete dialogue recordings to see this report.')
  index.row_factory=sqlite3.Row
  total=index.execute('select count(*) from finished_report'+where,parameters).fetchone()[0]
  start=min(int(offset),((total-1)//REPORT_PAGE_SIZE)*REPORT_PAGE_SIZE) if total else 0
  rows=index.execute(
   'select slot,status,title,speaker,reason,missing,seconds from finished_report'+
   where+' order by position limit ? offset ?', [*parameters,REPORT_PAGE_SIZE,start])
  return {'items':[dict(row) for row in rows],'total':total,'offset':start,'page_size':REPORT_PAGE_SIZE}

def complete_audio_path(app,data):
 ident=data.get('export_id','');position=str(data.get('index',''))
 if not isinstance(ident,str) or not re.fullmatch('[a-f0-9]{32}',ident) or not re.fullmatch('[0-9]+',position):raise ValueError('Choose a completed library and dialogue.')
 jobdir=app.folder/'exports'/ident;root=jobdir/'library';database=jobdir/'index.sqlite'
 if not database.is_file() or not (jobdir/'voxweave-audio-library.zip').is_file():raise ValueError('That completed library is not available.')
 with sqlite3.connect('file:'+quote(str(database.resolve()))+'?mode=ro',uri=True) as index:
  row=index.execute('select value from annotated where record=?',(int(position),)).fetchone()
 item=json.loads(row[0]).get('complete_dialogue') if row else None
 if not item or item.get('export_status')!='available':raise ValueError('This dialogue has no finished recording in that library.')
 path=(root/item['audio_path']).resolve()
 if not path.is_relative_to(root.resolve()) or not path.is_file() or file_hash(path)!=item['audio_sha256']:raise ValueError('The completed recording is missing or changed. Rebuild the library.')
 return path
