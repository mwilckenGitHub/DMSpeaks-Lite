"""Corpus adapters and source-preserving substitution model. No TTS dependencies."""
from __future__ import annotations
from engine_limits import MAX_SEGMENTS
import copy,csv,gzip,hashlib,html,io,json,re,sqlite3,threading,time,uuid
from bisect import bisect_left
from pathlib import Path
from entry_identity import identity as entry_identity
from settings_store import read_setting, write_setting

ALIASES={
 'id':['corpus_line_id','dialogue_id','lineId','stable_id','id','key'], 'text':['originalText','source.text','source_text','dialogue_text','text','dialogue','content'],
 'npc_id':['npc_id','npcId','speaker_id'], 'npc_name':['npc_name','npcName','speaker','speaker_name','name'],
 'npc_race':['npc_race','race'], 'npc_gender':['npc_gender','gender'], 'npc_type':['npc_type','npcType'],
 'npc_flavor':['npc_flavor','flavor'], 'npc_voice_profile':['npc_voice_profile','voice_profile','voice','voice_id'],
 'npc_voice_variant':['npc_voice_variant','voice_variant'], 'quest_id':['quest_id','questId','metadata.quest_id'],
 'quest_title':['quest_title','questTitle','title','metadata.quest_title'], 'dialogue_stage':['dialogue_stage','source','stage','metadata.dialogue_stage'],
 'dialogue_language':['dialogue_language','source.locale','language','locale'],
}
TOKEN=re.compile(r'\$[gG][^:;]*:[^;]*;|\$[nNcCrR]|\$\{[^{}\s]+\}|\{\{[^{}\s]+\}\}|\{\$[^{}\s]+\}')
BROKEN=re.compile(r'\$[gG](?![^:;]*:[^;]*;)|\$[A-Za-z](?![A-Za-z])')
DIRECTION=re.compile(r'\[[^\]\n]*\]')
MAX_STAGED_CORPORA=4

def getfield(row,path,default=None):
 if not path:return default
 if path in row:return row[path]
 value=row
 for part in path.split('.'):
  if not isinstance(value,dict) or part not in value:return default
  value=value[part]
 return value

def source_dialogue(row,field,slots):
 if field=='@segments':return ''.join(str(row.get(key) or '') for key in slots)
 return str(getfield(row,field,'') or '')

def setfield(row,path,value):
 if path in row or '.' not in path:row[path]=value;return
 target=row
 parts=path.split('.')
 for part in parts[:-1]:
  if not isinstance(target.get(part),dict):target[part]={}
  target=target[part]
 target[parts[-1]]=value

def flatten(row,prefix=''):
 result={}
 for key,value in row.items():
  name=prefix+str(key)
  if isinstance(value,dict):result.update(flatten(value,name+'.'))
  else:result[name]=value
 return result

def spoken_view(text):
 """Decoded speech text plus original Unicode source spans for each character."""
 output=[];spans=[];cursor=0
 controls=re.compile(r'\[[^\]\n]*\]|<[^>]+>|\$[bB]|&(?:#(?:x[0-9A-Fa-f]+|[0-9]+)|[A-Za-z][A-Za-z0-9]+);')
 def plain(start,end):
  output.extend(text[start:end]);spans.extend((i,i+1) for i in range(start,end))
 for match in controls.finditer(text):
  plain(cursor,match.start());token=match[0]
  if token.startswith('['):value=''
  elif token.startswith('<'):value=' ' if re.match(r'</?(?:p|s)\b|<break\b',token,re.I) else ''
  elif token.startswith('$'):value='\n'
  else:value=html.unescape(token)
  output.extend(value);spans.extend([(match.start(),match.end())]*len(value));cursor=match.end()
 plain(cursor,len(text));return ''.join(output),spans

def clean_spoken(text):
 return re.sub(r'\s+',' ',spoken_view(text)[0]).strip()

def token_value(token,player):
 if token.startswith(('${','{$')):key=token[2:-1]
 elif token.startswith('{{'):key=token[2:-2]
 else:
  key={'n':'name','r':'race','c':'class','g':'gender'}[token[1].lower()]
  if key=='gender':return key,token[2:-1].split(':',1)[1 if player.get('gender')=='female' else 0].strip()
 raw=player.get(key,'');value='' if raw is None else ('true' if raw is True else 'false' if raw is False else str(raw))
 if key!='name' and token.startswith('$') and len(token)==2 and token[1].isupper():value=value[:1].upper()+value[1:]
 return key,value

def delimiter_pattern(delimiters):
 custom={d['representation'] for d in delimiters or []}
 return re.compile('|'.join(re.escape(s) for s in sorted(custom,key=len,reverse=True))+'|'+TOKEN.pattern) if custom else TOKEN

def parse_segments(text,player=None,delimiters=None,*,source_controls=()):
 defaults={d['key']:d['default'] for d in delimiters or []
           if d.get('type')!='formatting' and d.get('default')}
 player={**defaults,**(player or {})};segments=[];cursor=0
 pattern=delimiter_pattern(delimiters)
 matches=list(pattern.finditer(text))
 formatting={d['representation'] for d in delimiters or [] if d.get('type')=='formatting'}
 format_ends={match.start():match.end() for match in matches if match[0] in formatting}
 inline_ends=dict(format_ends)
 if source_controls:
  format_ends.update({start:end for start,end,_ in source_controls})
  inline_ends.update({start:end for start,end,rule in source_controls if rule['type']=='formatting'})
  format_ends=dict(sorted(format_ends.items()))
 format_starts=list(format_ends)
 def skip_inline(at):
  while at in inline_ends:at=inline_ends[at]
  return at
 def possessive(begin):
  apostrophe=skip_inline(begin)
  if text[apostrophe:apostrophe+1] not in ("'",'’'):return '',begin
  letter=skip_inline(apostrophe+1)
  if text[letter:letter+1] not in ('s','S'):return '',begin
  following=skip_inline(letter+1)
  if re.match(r'\w',text[following:following+1]):return '',begin
  return text[apostrophe]+text[letter],letter+1
 def add_fixed(begin,end):
  raw=text[begin:end]
  if not raw:return
  pieces=[];at=begin
  for i in range(bisect_left(format_starts,begin),bisect_left(format_starts,end)):
   start=format_starts[i];pieces.append(text[at:start]);at=format_ends[start]
  pieces.append(text[at:end])
  resolved=''.join(re.sub(r'\$[bB]','\n',piece) for piece in pieces)
  if not re.search(r'\w',clean_spoken(resolved)):
   if segments:
    segments[-1]['source_end']=end;segments[-1]['source_text']+=raw;segments[-1]['resolved_text']+=resolved
   if segments:return
  segments.append({'type':'fixed','placeholder':'','value':'','source_start':begin,'source_end':end,'source_text':raw,'resolved_text':resolved})
 custom={d['representation']:d['key'] for d in (delimiters or [])
         if not TOKEN.fullmatch(d['representation']) or token_value(d['representation'],{})[0]!=d['key']}
 for match in matches:
  if match[0] in formatting:continue
  add_fixed(cursor,match.start())
  if match[0] in custom:
   kind=custom[match[0]];raw=player.get(kind,'');value='' if raw is None else ('true' if raw is True else 'false' if raw is False else str(raw))
  else:kind,value=token_value(match[0],player)
  literal,end=possessive(match.end())
  segment={'type':kind,'placeholder':match[0],'value':value,'source_start':match.start(),'source_end':end,
           'source_text':text[match.start():end],'resolved_text':value+literal if value.strip() else ''}
  if literal:
   segment['literal_suffix']=literal
   if end!=match.end()+len(literal):segment['lexical_source_end']=end
  segments.append(segment)
  cursor=end
 add_fixed(cursor,len(text))
 if any(s.get('literal_suffix') for s in segments):
  # All pieces on this line changed lexical partitioning. Do not reuse older
  # extracted fixed speech that may still contain the detached ending.
  for s in segments:s['segment_model']='lexical-affix-1'
 for i,s in enumerate(segments,1):s['order']=i;s['spoken_text']=clean_spoken(s['resolved_text'])
 resolved=''.join(s['resolved_text'] for s in segments)
 warnings=[]
 if len(segments)>MAX_SEGMENTS:warnings.append(f'{len(segments)} pieces exceeds the current runtime capacity of {MAX_SEGMENTS}. The complete source is retained.')
 remainder=pattern.sub('',text);remainder=re.sub(r'\$[bB]','',remainder)
 if re.search(r'\$[gG]',remainder):warnings.append('An unfinished gender delimiter was found; use $gmale:female;.')
 if re.search(r'\$[A-Za-z]',remainder):warnings.append('Unrecognized game placeholder found; it will not be guessed or silently replaced.')
 if text.count('[')!=text.count(']'):warnings.append('An unfinished direction bracket was found.')
 return {'segments':segments,'resolved_text':resolved,'spoken_text':clean_spoken(resolved),'warnings':warnings,'missing_values':[s['placeholder'] for s in segments if s['type']!='fixed' and not s['value'].strip()],**({'formatting_spans':format_ends} if format_ends else {})}

def delimiter_source_end(segment):
 """End of the delimiter and its spoken suffix on the original text clock."""
 return segment.get('lexical_source_end',segment.get('source_start',0)+
                    len(segment.get('placeholder',''))+len(segment.get('literal_suffix','')))

def char_word_mapping(resolved_text,segments):
 result=[]
 for segment in segments:
  if 'speech_word_mapping' in segment:
   result.extend(copy.deepcopy(segment['speech_word_mapping']));continue
  raw=segment['source_text'] if segment['type']=='fixed' else segment['resolved_text']
  spoken,spans=spoken_view(raw)
  for word in re.finditer(r"[^\W_]+(?:['’][^\W_]+)*",spoken,re.UNICODE):
   if segment['type']=='fixed':begin=segment['source_start']+spans[word.start()][0];end=segment['source_start']+spans[word.end()-1][1]
   else:begin=segment['source_start'];end=delimiter_source_end(segment)
   result.append({'word':word[0],'source_start':begin,'source_end':end,'segment_order':segment['order']})
 return result

def segment_audio_identity(segment):
 return {'segment_model':segment.get('segment_model','legacy'),'literal_suffix':segment.get('literal_suffix',''),**({'performance_model':segment['performance_model']} if segment.get('performance_model') else {})}

def segment_audio_matches(asset,segment):
 return all(asset.get(key,'legacy' if key=='segment_model' else '')==value for key,value in segment_audio_identity(segment).items())

def decode_corpus(data,name,*,collect_tei_styles=False):
 if data[:2]==b'\x1f\x8b':
  with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:data=f.read(80_000_001)
 if len(data)>80_000_000:raise ValueError('The expanded corpus exceeds 80 MB.')
 lower_name=name.lower()
 styled_drafts=None
 if lower_name.endswith(('.tei','.xml')):
  from tei_corpus_import import read_document
  # Keep the working rows compatible with the JSON editor and exporters.
  # The store and project archive still retain the original TEI bytes.
  styled_drafts={} if collect_tei_styles else None
  rows=read_document(data,styled_drafts=styled_drafts)
  root=rows;kind='json';container=None
  fields=list(dict.fromkeys(k for row in rows for k in flatten(row)))
 elif lower_name.endswith('.csv'):
  text=data.decode('utf-8-sig')
  reader=csv.DictReader(io.StringIO(text,newline=''))
  if not reader.fieldnames or len(reader.fieldnames)!=len(set(reader.fieldnames)):raise ValueError('CSV headers must be present and unique.')
  rows=list(reader)
  if any(None in row for row in rows):raise ValueError('A CSV row contains more cells than its header.')
  root=rows;kind='csv';container=None;fields=reader.fieldnames
 else:
  root=json.loads(data.decode('utf-8-sig'));container=None;kind='json'
  if isinstance(root,dict) and root.get('format')=='open-speech-corpus':
   from osc_validator import validate_corpus
   errors=validate_corpus(root)
   if errors:raise ValueError('Open Speech Corpus validation: '+'; '.join(errors[:8]))
  if isinstance(root,list):rows=root
  elif isinstance(root,dict):
   container=next((k for k in ['lines','records','entries','items','dialogues'] if isinstance(root.get(k),list)),None)
   if container is None:raise ValueError('Use a JSON array or an object containing lines, records, entries, items, or dialogues.')
   rows=root[container]
  else:raise ValueError('The JSON corpus must contain records.')
  if not all(isinstance(x,dict) for x in rows):raise ValueError('Every corpus record must be an object.')
  fields=list(dict.fromkeys(k for row in rows for k in flatten(row)))
 if not rows:raise ValueError('This corpus has no dialogue records.')
 if len(rows)>200_000:raise ValueError('This prototype supports up to 200,000 records per import.')
 mapping={key:next((x for x in aliases if x in fields),'') for key,aliases in ALIASES.items()}
 slots=sorted([h for h in fields if re.fullmatch(r'segment_\d+',h)],key=lambda h:int(h.split('_')[-1]))
 if not mapping['text'] and slots:mapping['text']='@segments'
 parsed={'root':root,'rows':rows,'kind':kind,'container':container,'fields':fields,'mapping':mapping,'slots':slots,'name':Path(name).name}
 from clip_group_corpus import read
 parsed['clip_group_config']=read(parsed)
 from corpus_lookup import read as read_lookup
 parsed['lookup']=read_lookup(parsed)
 if styled_drafts is not None:parsed['tei_style_drafts']=styled_drafts
 return parsed

def record_metadata(data,index):
 raw=data['rows'][index];mapping=data['mapping'];flat=flatten(raw)
 meta={k:getfield(raw,v) for k,v in mapping.items() if k not in ('id','text') and v}
 speaker={};root=data['root']
 from dialogue_language import resolve as resolve_language
 language=resolve_language(raw,root,meta.get('dialogue_language'))
 if language:meta['dialogue_language']=language
 if isinstance(root,dict) and root.get('format')=='open-speech-corpus':
  speaker=copy.deepcopy(next((s for s in root.get('speakers',[]) if s.get('id')==raw.get('speaker_ref')),{}))
  if speaker:
   for key,value in raw.get('metadata',{}).items():
    if key.startswith('npc_') and key not in {'npc_id','npc_voice_profile'}:meta.setdefault(key,copy.deepcopy(value))
   meta.setdefault('npc_id',speaker['id']);meta.setdefault('npc_name',speaker.get('name',speaker['id']))
   for key,value in speaker.get('metadata',{}).items():
    if key.startswith('npc_') and key not in {'npc_id','npc_name','npc_voice_profile'}:meta.setdefault(key,copy.deepcopy(value))
   flat.update({'speaker.'+k:v for k,v in flatten(speaker).items()})
  voice_ref=raw.get('voice_profile_ref') or speaker.get('voice_profile_ref')
  if voice_ref:meta.setdefault('npc_voice_profile',voice_ref)
 if not speaker:speaker={'id':meta.get('npc_id'),'name':meta.get('npc_name'),'voice_profile_ref':meta.get('npc_voice_profile')}
 return meta,speaker,flat

class Store:
 def __init__(self,folder):
  self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True);self.lock=threading.RLock();self.cache={};self.original_cache={};self.staged={}
  self.db=sqlite3.connect(self.folder/'studio.sqlite',check_same_thread=False)
  self.db.execute('pragma journal_mode=WAL')
  self.db.executescript('''create table if not exists datasets(id text primary key,name text,kind text,mapping text,created real);
create table if not exists drafts(dataset text,record integer,text text,updated real,primary key(dataset,record));
create table if not exists settings(key text primary key,value text);
create table if not exists assets(id text primary key,metadata text);
create table if not exists takes(id text primary key,metadata text);
create index if not exists assets_piece_lookup on assets(json_extract(metadata,'$.kind'),json_extract(metadata,'$.line_id')) where json_valid(metadata);
create index if not exists assets_non_json_lookup on assets(id) where not json_valid(metadata);
create index if not exists takes_record_lookup on takes(json_extract(metadata,'$.record_id')) where json_valid(metadata);
create index if not exists takes_non_json_lookup on takes(id) where not json_valid(metadata);
create table if not exists corpus_overlays(dataset text primary key,root text,mapping text,revision integer);
create table if not exists corpus_metadata_history(id integer primary key autoincrement,dataset text,command text,inverse text,undone integer default 0,created real);
create table if not exists corpus_structure_history(dataset text primary key,revision integer not null,undo text not null,redo text not null);''');self.db.commit()
  from authoring_catalog import attach
  attach(self)
 def stage(self,data,name,*,preserve_tei_styles=True):
  parsed=decode_corpus(data,name,collect_tei_styles=preserve_tei_styles);token=uuid.uuid4().hex
  preview=parsed['rows'][:2]
  stage={'staged_id':token,'name':parsed['name'],'count':len(parsed['rows']),'fields':parsed['fields'],'mapping':parsed['mapping'],'preview':preview,'format':parsed['kind']}
  if parsed['slots']:
   stage['ordered_segment_samples']=[source_dialogue(row,'@segments',parsed['slots'])[:601] for row in preview]
  with self.lock:
   # A closed browser may leave an inspection behind. Keep only recent stages.
   if len(self.staged)>=MAX_STAGED_CORPORA:
    self.staged.pop(next(iter(self.staged)))
   self.staged[token]=(parsed,data)
  return stage
 def staged_corpus(self,token):
  if not isinstance(token,str):raise ValueError('Choose a staged corpus.')
  with self.lock:
   if token not in self.staged:raise ValueError('The staged import expired; choose the file again.')
   return self.staged[token]
 def discard_staged(self,token):
  if not isinstance(token,str):raise ValueError('Choose a staged corpus.')
  with self.lock:return self.staged.pop(token,None) is not None
 def import_staged(self,token,mapping=None):
  with self.lock:
   parsed,raw=self.staged_corpus(token)
   imported=self.import_parsed(parsed,raw,mapping)
   self.discard_staged(token)
   return imported
 def import_parsed(self,parsed,raw,mapping=None):
  """Install decoded source rows without modifying the staged inspection."""
  mapping=dict(mapping or parsed['mapping'])
  if mapping.get('text')!='@segments' and mapping.get('text') not in parsed['fields']:raise ValueError('Choose a source-text field before importing.')
  for catalog in parsed.get('clip_group_config',{}).get('dialogues',[]):
   value=getfield(parsed['rows'][catalog['index']],mapping.get('id'))
   identity=value if value not in (None,'') else f'row-{catalog["index"]+1}'
   if catalog['dialogue_id']!=identity:raise ValueError('Replacement groups belong to another dialogue. Check the identity field mapping.')
  ident=hashlib.sha256(raw+json.dumps(mapping,sort_keys=True).encode()).hexdigest()[:20]
  # A fresh TEI import initializes styles even if no row uses them yet.
  # A project restore supplies its own overlay and drafts from the archive.
  styled_drafts=parsed.get('tei_style_drafts') if mapping.get('text')=='text' else None
  if styled_drafts is not None and any(len(text)>50_000 for text in styled_drafts.values()):
   raise ValueError('A styled TEI dialogue exceeds the 50,000-character draft limit.')
  if styled_drafts is not None:
   from corpus_lookup import stage_root
   from tei_inline import formatting_rules
   styled_root,_,_=stage_root(parsed,{**parsed['lookup'],'rules':formatting_rules()})
  with self.lock:
   file=self.folder/(ident+'.original');file.write_bytes(raw)
   now=time.time()
   with self.db:
    inserted=self.db.execute('insert or ignore into datasets values(?,?,?,?,?)',(ident,parsed['name'],parsed['kind'],json.dumps(mapping),now))
    if inserted.rowcount and styled_drafts is not None:
     self.db.execute('insert into corpus_overlays values(?,?,?,?)',
                     (ident,json.dumps(styled_root,ensure_ascii=False),json.dumps(mapping),1))
     self.db.executemany('insert into drafts values(?,?,?,?)',
                         ((ident,index,text,now) for index,text in styled_drafts.items()))
   original={**parsed,'mapping':mapping}
   original.pop('tei_style_drafts',None)
   self.original_cache[ident]=original;self.cache.pop(ident,None)
  return self.dataset_info(ident)
 def original_load(self,ident):
  with self.lock:
   if ident not in self.original_cache:
    row=self.db.execute('select name,mapping from datasets where id=?',(ident,)).fetchone()
    if not row:raise ValueError('Unknown corpus.')
    parsed=decode_corpus((self.folder/(ident+'.original')).read_bytes(),row[0]);parsed['mapping']=json.loads(row[1]);parsed['overlay_revision']=0;self.original_cache[ident]=parsed
   return self.original_cache[ident]
 def load(self,ident):
  with self.lock:
   original=self.original_load(ident);version=self.db.execute('select revision from corpus_overlays where dataset=?',(ident,)).fetchone();revision=version[0] if version else 0
   if ident not in self.cache or self.cache[ident].get('overlay_revision',0)!=revision:
    if version:
     from corpus_overlay import working_view
     row=self.db.execute('select root,mapping from corpus_overlays where dataset=?',(ident,)).fetchone();self.cache[ident]=working_view(original,json.loads(row[0]),json.loads(row[1]),revision)
    else:self.cache[ident]=original
   return self.cache[ident]
 def overlay(self,ident):
  self.original_load(ident)
  with self.lock:row=self.db.execute('select root,mapping,revision from corpus_overlays where dataset=?',(ident,)).fetchone()
  return {'root':json.loads(row[0]),'mapping':json.loads(row[1]),'revision':row[2]} if row else None
 def datasets(self):
  with self.lock:return [self.dataset_info(x[0]) for x in self.db.execute('select id from datasets order by created desc').fetchall()]
 def dataset_name(self,ident):
  with self.lock:
   row=self.db.execute('select name from datasets where id=?',(ident,)).fetchone()
  return row[0] if row else None
 def dataset_info(self,ident):
  data=self.load(ident);return {'id':ident,'name':data['name'],'count':len(data['rows']),'format':data['kind'],'mapping':data['mapping']}
 def draft_map(self,ident):
  with self.lock:return dict(self.db.execute('select record,text from drafts where dataset=?',(ident,)).fetchall())
 def record(self,ident,index):
  data=self.load(ident)
  return self._record_from_views(ident,index,data,self.original_load(ident))
 def _record_from_views(self,ident,index,data,original):
  if index<0 or index>=len(data['rows']):raise ValueError('Unknown record.')
  raw=data['rows'][index];mapping=data['mapping'];flat=flatten(raw)
  source=source_dialogue(raw,mapping['text'],data['slots'])
  drafts=self.draft_map(ident);text=drafts.get(index,source)
  meta,speaker,flat=record_metadata(data,index)
  oldmeta,oldspeaker,oldflat=record_metadata(original,index)
  from corpus_lookup import EXTENSION,FIELD,ACTIVE,marker_rules
  from delimiter_definitions import INFERRED_COMMENTS_FIELD
  from entry_delimiter_edit import ENTRY_AUDIO_ANCHOR,restore_entry_audio_fields
  for fields,parsed in ((flat,data),(oldflat,original)):
   for key in tuple(fields):
    if key in {EXTENSION,FIELD,ACTIVE} or key.startswith(EXTENSION+'.'):fields.pop(key)
   rules=marker_rules(parsed['lookup'],text)
   if rules:fields[ACTIVE]=rules
  def signature(parsed,metadata,actor,fields,row_index):
   omitted={path for path in parsed['mapping'].values() if path}|set(parsed['slots']);extra={k:v for k,v in fields.items() if k not in omitted and not (k=='_dmspeaks_clip_groups' or k.startswith('_dmspeaks_clip_groups.') or k=='_dmspeaks_clip_groups_json')}
   restore_entry_audio_fields(extra)
   extra.pop(INFERRED_COMMENTS_FIELD,None)
   return hashlib.sha256(json.dumps({'id':getfield(parsed['rows'][row_index],parsed['mapping'].get('id')),'metadata':metadata,'speaker':actor,'extra':extra},sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]
  metadata_revision=signature(data,meta,speaker,flat,index);baseline_revision=signature(original,oldmeta,oldspeaker,oldflat,index)
  record={'metadata_revision':metadata_revision,'metadata_changed':metadata_revision!=baseline_revision,'corpus_revision':data.get('overlay_revision',0),'speaker':speaker,'dataset':ident,'index':index,'id':getfield(raw,mapping.get('id')) if getfield(raw,mapping.get('id')) not in (None,'') else f'row-{index+1}','source_text_field':mapping.get('text'),'original_text':source,'text':text,'edited':text!=source,'metadata':meta,'all_metadata':{k:v for k,v in flat.items() if k!=mapping.get('text') and k not in data['slots']},'revision':hashlib.sha256(text.encode()).hexdigest()[:16]}
  record['all_metadata'].pop(ENTRY_AUDIO_ANCHOR,None)
  from source_text_lock import is_locked
  from clip_ownership import source_id_counts
  from authoring_catalog import canonical_key
  source_key=canonical_key(record['id'])
  record['source_id_ambiguous']=source_key is None or source_id_counts(data,getfield).get(source_key,0)>1
  record.update(entry_identity(ident,index,getfield(raw,mapping.get('id'))))
  record['source_text_locked']=is_locked(self,ident)
  from tei_inline import formatting_styles
  record['formatting_styles']=formatting_styles(data['lookup']['rules']) if mapping['text']!='@segments' else []
  if record['formatting_styles']:
   from performance_events import declared_key
   if declared_key(record) is not None:
    record['formatting_read_only_reason']='This entry has authored cues tied to exact text positions.'
  from project_delimiter_library import audio_definition,read as read_project_delimiters
  record['project_delimiters']=read_project_delimiters(self,ident)
  if any(audio_definition(item) is not None for item in record['project_delimiters']):
   # Shared definitions affect parsing and audio compatibility only where used.
   from delimiter_definitions import registered
   definitions=registered(record)
   used={m[0] for m in delimiter_pattern(definitions).finditer(text)}
   relevant=[fields for d in definitions if d['representation'] in used
             if (fields:=audio_definition(d)) is not None]
   if relevant:
    record['metadata_revision']=hashlib.sha256(json.dumps([metadata_revision,relevant],sort_keys=True,ensure_ascii=False).encode()).hexdigest()[:20]
    record['metadata_changed']=True
  from delimiter_definitions import enrich_record
  enriched=enrich_record(record)
  enriched['all_metadata'].pop(INFERRED_COMMENTS_FIELD,None)
  return enriched
 def _structure_views(self,ident,slot,snapshot,*,include_hidden=False):
  """Construct detached record views without changing the imported corpus."""
  from project_delimiter_library import read as read_project_delimiters
  state=snapshot['state'];count=snapshot['original_count']
  if type(slot) is not int or slot<0:raise ValueError('Unknown record.')
  if slot not in state['order'] and not (include_hidden and slot in state['hidden']):raise ValueError('This entry is not visible.')
  data=self.load(ident);original=self.original_load(ident);identity_uncertain=False
  if state['next_slot']>count:
   added=[];mapping=data['mapping'];identity_uses_segment=mapping.get('id') in data['slots']
   for index in range(count,state['next_slot']):
    content=snapshot['added_entries'][str(index)];row=copy.deepcopy(content['metadata'])
    if mapping['text']!='@segments':setfield(row,mapping['text'],content['text'])
    elif index==slot or identity_uses_segment:
     try:self._apply_export_text(data,row,content['text'],read_project_delimiters(self,ident))
     except ValueError:
      if index==slot:raise
      identity_uncertain=True
    added.append(row)
   data={**data,'rows':[*data['rows'],*added]};data.pop('_clip_source_id_counts',None)
   original={**original,'rows':[*original['rows'],*added]};original.pop('_clip_source_id_counts',None)
   # Metadata editing uses root-relative pointers. Keep this root detached too.
   if data['container'] is None:data['root']=data['rows']
   else:data['root']={**data['root'],data['container']:data['rows']}
  return data,original,identity_uncertain
 def record_structure(self,ident,slot,*,include_hidden=False):
  """Internal stable-slot preview; new-entry editing is not exposed yet."""
  from corpus_structure_commands import read_state
  with self.lock:
   own_transaction=not self.db.in_transaction
   if own_transaction:self.db.execute('BEGIN')
   try:
    structure=read_state(self,ident);snapshot=structure['snapshot']
    data,original,identity_uncertain=self._structure_views(ident,slot,snapshot,include_hidden=include_hidden)
    record=self._record_from_views(ident,slot,data,original)
    if identity_uncertain:record['source_id_ambiguous']=True
    record.update(structure_revision=structure['revision'],new_entry=slot>=snapshot['original_count'])
    if own_transaction:self.db.commit()
    return record
   except BaseException:
    if own_transaction:self.db.rollback()
    raise
 def search(self,ident,q='',npc='',stage='',status='',offset=0,limit=50,*,in_context=False,
            include_match_preview=False,known_speaker_list=None,in_metadata=False,_structure=None):
  if in_context and in_metadata:raise ValueError('Choose one search scope.')
  data=self.load(ident);mapping=data['mapping'];drafts=self.draft_map(ident)
  matches=[];npcs=set();stages=set();words=q.casefold().split()
  search_internal_ids=any(word.startswith('dms-') for word in words)
  term=q.strip()
  pattern=re.compile(re.escape(term),re.IGNORECASE) if in_context and term else None
  metadata_pattern=re.compile(re.escape(term),re.IGNORECASE) if in_metadata and term else None
  if metadata_pattern:
   from json_fields import escape, find_metadata_match, mapped_pointer
   source_field=mapping['text']
   # Corpora such as WoW carry both the original and the prepared dialogue.
   source_slots={'/'+escape(key) for key in [*data['slots'],*ALIASES['text']]}
   source_slots.update('/'+'/'.join(map(escape,key.split('.'))) for key in ALIASES['text'] if '.' in key)
  preview_pattern=re.compile(re.escape(term),re.IGNORECASE) if include_match_preview and not (in_context or in_metadata) and term else None
  speakers={}
  root=data['root']
  if isinstance(root,dict) and root.get('format')=='open-speech-corpus':
   for speaker in root.get('speakers',[]):speakers.setdefault(speaker['id'],speaker.get('name',speaker['id']))
  total=0
  if _structure is None:
   entry_rows=((index,raw,None,False) for index,raw in enumerate(data['rows']))
  else:
   structure=_structure['snapshot'];count=structure['original_count']
   def stable_rows():
    definitions=None
    for slot in structure['state']['order']:
     if slot<count:
      yield slot,data['rows'][slot],None,False
      continue
     entry=structure['added_entries'][str(slot)];row=copy.deepcopy(entry['metadata']);invalid=False
     if mapping['text']=='@segments':
      try:
       if definitions is None:
        from project_delimiter_library import read as read_project_delimiters
        definitions=read_project_delimiters(self,ident)
       self._apply_export_text(data,row,entry['text'],definitions)
      except ValueError:
       invalid=True
       for key in data['slots']:row.pop(key,None)
     else:setfield(row,mapping['text'],entry['text'])
     yield slot,row,entry['text'],invalid
   entry_rows=stable_rows()
  for index,raw,added_text,invalid_added_text in entry_rows:
   who=str(getfield(raw,mapping.get('npc_name'),'') or '');when=str(getfield(raw,mapping.get('dialogue_stage'),'') or '')
   if not who and speakers:
    speaker_ref=raw.get('speaker_ref')
    if isinstance(speaker_ref,str):who=speakers.get(speaker_ref,'')
   if who:npcs.add(who)
   if when:stages.add(when)
   if npc and npc!=who:continue
   if stage and stage!=when:continue
   edited=added_text is not None or index in drafts
   if status=='edited' and not edited:continue
   if status=='original' and edited:continue
   text=drafts.get(index,added_text)
   metadata_match=None
   if metadata_pattern:
    excluded=source_slots if source_field=='@segments' else source_slots | {mapped_pointer(raw,source_field)}
    metadata_match=find_metadata_match(raw,excluded,metadata_pattern)
    if not metadata_match:continue
   elif words and text is None:
    text=source_dialogue(raw,mapping['text'],data['slots'])
   if not pattern and not metadata_pattern and words:
    fields=(getfield(raw,mapping.get(k)) for k in ['id','npc_id','npc_name','quest_id','quest_title'])
    searchable=' '.join('' if value is None else str(value) for value in fields)+' '+who+' '+text
    if search_internal_ids:searchable+=' '+entry_identity(ident,index,None)['internal_id']
    folded=searchable.casefold()
    if not all(w in folded for w in words):continue
   occurrences=pattern.finditer(text) if pattern else (None,)
   for occurrence in occurrences:
    total+=1
    if not offset<total<=offset+limit:continue
    entry_id=getfield(raw,mapping.get('id'))
    result={'index':index,'id':entry_id if entry_id not in (None,'') else f'row-{index+1}',
            'npc_name':who,'quest_id':getfield(raw,mapping.get('quest_id')),'stage':when,'edited':edited}
    result.update(entry_identity(ident,index,entry_id))
    if metadata_match:result['metadata_match']=metadata_match
    if invalid_added_text:result['entry_needs_repair']=True
    if occurrence:
     start,end=occurrence.span()
     result['context']={'before':text[max(0,start-45):start],'match':occurrence[0],'after':text[end:end+45],
                        'start':start,'end':end,'revision':hashlib.sha256(text.encode()).hexdigest()[:16]}
    else:
     if text is None:text=source_dialogue(raw,mapping['text'],data['slots'])
     result['preview']=re.sub(r'\s+',' ',text)[:180]
     match=preview_pattern.search(text) if preview_pattern else None
     if match:
      start,end=match.span()
      result['match_preview']={'before':text[max(0,start-35):start],'match':match[0],'after':text[end:end+80]}
    matches.append(result)
  result={'records':matches,'total':total,'offset':offset,'limit':limit,'npcs':sorted(npcs),'stages':sorted(stages)}
  if known_speaker_list is not None:
   speakers=json.dumps(result['npcs'],separators=(',',':')).encode()
   version=hashlib.sha256(speakers).hexdigest()
   result['speaker_list_version']=version
   if known_speaker_list==version:result.pop('npcs')
  if _structure is not None:result['structure_revision']=_structure['revision']
  return result
 def search_structure(self,ident,q='',npc='',stage='',status='',offset=0,limit=50,*,in_context=False,
                      include_match_preview=False,include_state=False,known_structure_revision=None,known_speaker_list=None,
                      in_metadata=False):
  """Search visible stable slots, optionally including the same snapshot's order."""
  from corpus_structure_commands import public_state, read_state
  with self.lock:
   own_transaction=not self.db.in_transaction
   if own_transaction:self.db.execute('BEGIN')
   try:
    structure=read_state(self,ident)
    result=self.search(ident,q,npc,stage,status,offset,limit,in_context=in_context,
                       include_match_preview=include_match_preview,known_speaker_list=known_speaker_list,
                       in_metadata=in_metadata,_structure=structure)
    if include_state:
     result['structure']=public_state(structure)
     if known_structure_revision==structure['revision']:
      result['structure'].pop('order')
      result['structure']['order_unchanged']=True
    if own_transaction:self.db.commit()
    return result
   except BaseException:
    if own_transaction:self.db.rollback()
    raise
 def save_draft(self,ident,index,text,*,expected_revision=None,expected_structure=None,require_dictionary=False):
  if not isinstance(text,str) or len(text)>50000:raise ValueError('A source string must be text of at most 50,000 characters.')
  with self.lock:
   if expected_structure is not None or require_dictionary:
    if self.db.in_transaction:raise ValueError('Finish the current project change first.')
    self.db.execute('BEGIN IMMEDIATE')
   try:
    if expected_structure is not None:
     from corpus_structure_commands import require_visible_original
     if not isinstance(expected_structure,dict) or expected_structure.get('index')!=index:raise ValueError('Invalid entry precondition.')
     require_visible_original(self,ident,expected_structure)
    record=self.record(ident,index)
    if expected_revision is not None and expected_revision!=record['revision']:
     raise ValueError('This dialogue was changed elsewhere. Copy your unsaved text before reopening the dialogue.')
    from source_text_lock import check_text
    check_text(self,ident,record['text'],text)
    if require_dictionary:
     from project_delimiter_library import validate_new_source_markers
     validate_new_source_markers(self,ident,record['text'],text)
    if text==record['original_text']:self.db.execute('delete from drafts where dataset=? and record=?',(ident,index))
    else:self.db.execute('insert or replace into drafts values(?,?,?,?)',(ident,index,text,time.time()))
    self.db.commit()
    return self.record(ident,index)
   except BaseException:
    if expected_structure is not None or require_dictionary:self.db.rollback()
    raise
 def preview_corpus_row(self,ident,index,text,*,values=None,include_row=True):
  """Show draft dialogue and field edits in the corpus shape without saving."""
  if not isinstance(text,str) or len(text)>50000:raise ValueError('A source string must be text of at most 50,000 characters.')
  with self.lock:
   return self._preview_corpus_data(ident,self.load(ident),index,text,values,include_row)
 def preview_structure_row(self,ident,index,text,*,values=None,include_row=True):
  """Preview a stable-slot entry without committing its text or fields."""
  from corpus_structure_commands import read_state
  if not isinstance(text,str) or len(text)>50000:raise ValueError('A source string must be text of at most 50,000 characters.')
  with self.lock:
   own_transaction=not self.db.in_transaction
   if own_transaction:self.db.execute('BEGIN')
   try:
    structure=read_state(self,ident)
    data,_,_=self._structure_views(ident,index,structure['snapshot'])
    result=self._preview_corpus_data(ident,data,index,text,values,include_row)
    result['structure_revision']=structure['revision']
    if own_transaction:self.db.commit()
    return result
   except BaseException:
    if own_transaction:self.db.rollback()
    raise
 def _preview_corpus_data(self,ident,data,index,text,values,include_row):
   if not 0<=index<len(data['rows']):raise ValueError('Unknown record.')
   row=data['rows'][index]
   if include_row:
    row=copy.deepcopy(row)
    if values is not None:
     from metadata_editor import apply_entry_values
     apply_entry_values(data,row,values)
   from delimiter_definitions import placeholders_in_text
   from project_delimiter_library import read as read_project_delimiters
   project_definitions=read_project_delimiters(self,ident)
   result={'placeholders':placeholders_in_text(text,flatten(row),project_definitions),'format':data['kind']}
   if not include_row:return result
   field=data['mapping']['text']
   original=''.join(str(row.get(key) or '') for key in data['slots']) if field=='@segments' else str(getfield(row,field,'') or '')
   if text!=original:self._apply_export_text(data,row,text,project_definitions)
   from entry_delimiter_edit import ENTRY_AUDIO_ANCHOR
   row.pop(ENTRY_AUDIO_ANCHOR,None)
   result['row']=row
   return result
 @staticmethod
 def _apply_export_text(data,row,text,project_definitions=None):
  mapping=data['mapping']
  if mapping['text']=='@segments':
   from delimiter_definitions import registered
   pieces=parse_segments(text,delimiters=registered({'all_metadata':flatten(row),'project_delimiters':project_definitions or []}))['segments']
   if len(pieces)>len(data['slots']):raise ValueError('Edited dialogue needs more segment columns than this CSV defines.')
   for n,key in enumerate(data['slots']):row[key]=pieces[n]['source_text'] if n<len(pieces) else ''
   return
  root=data['root']
  if isinstance(root,dict) and root.get('format')=='open-speech-corpus':
   prior=getfield(row,mapping['text'],'');row.setdefault('source_history',{})[hashlib.sha256(prior.encode()).hexdigest()]=prior
  setfield(row,mapping['text'],text)
 def export(self,ident,project=False,*,include_group_state=True,_include_structure=True):
  if _include_structure:
   from corpus_structure_snapshot import read as read_entry_structure
   if read_entry_structure(self,ident) is not None:
    if project:raise ValueError('Use the project exporter to preserve entry structure.')
    from corpus_structure_export import export as export_structured_corpus
    result=export_structured_corpus(self,ident)
    return result['data'],result['media_type'],result['name']
  data=self.load(ident);root=copy.deepcopy(data['root']);rows=root if data['container'] is None else root[data['container']];drafts=self.draft_map(ident);mapping=data['mapping']
  if project:return json.dumps({'format':'dialogue-studio-project','version':1,'original_corpus':data['root'],'corpus_format':data['kind'],'mapping':mapping,'drafts':{str(k):v for k,v in drafts.items()},'settings':{k:self.setting(k) for k in ['characters','layout','theme']}},ensure_ascii=False,indent=2).encode(),'application/json','dialogue-studio-project.json'
  from entry_delimiter_edit import ENTRY_AUDIO_ANCHOR
  for row in rows:row.pop(ENTRY_AUDIO_ANCHOR,None)
  from project_delimiter_library import read as read_project_delimiters
  project_definitions=read_project_delimiters(self,ident)
  for i,text in drafts.items():
   if i>=len(rows):
    from corpus_structure_snapshot import read as read_entry_structure
    structure=read_entry_structure(self,ident) if not _include_structure else None
    if structure is None or str(i) not in structure['added_entries']:raise ValueError('Draft references an unknown entry.')
    continue  # The structure-aware exporter applies this draft to its added row.
   self._apply_export_text(data,rows[i],text,project_definitions)
  # A corpus export has no project file; attach used project definitions to
  # exported entries so custom literal markers remain interpretable on import.
  if project_definitions:
   from delimiter_definitions import FIELD, dictionary_definition, local_registered
   for row in rows:
    text=''.join(str(row.get(k) or '') for k in data['slots']) if mapping['text']=='@segments' else str(getfield(row,mapping['text'],'') or '')
    used={match[0] for match in delimiter_pattern(project_definitions).finditer(text)}
    relevant=[item for item in project_definitions if item['representation'] in used]
    if not relevant:continue
    local=local_registered({'all_metadata':flatten(row)})
    from project_delimiter_resolution import resolve
    merged=[dictionary_definition(item) for item in resolve(relevant,local)]
    field=FIELD+('_json' if data['kind']=='csv' else '')
    row[field]=json.dumps(merged,ensure_ascii=False) if data['kind']=='csv' else merged
  from audio_sources import embed,registry
  embed(root,registry(self,ident),data['kind'],data['container'])
  if include_group_state:
   from clip_groups import export_config
   from clip_group_corpus import embed as embed_groups
   embed_groups(root,export_config(self,ident),data['kind'],data['container'])
  if data['kind']=='csv':
   fields=[key for key in data['fields'] if key!=ENTRY_AUDIO_ANCHOR]
   buffer=io.StringIO(newline='');writer=csv.DictWriter(buffer,fieldnames=list(dict.fromkeys([*fields,*(k for row in rows for k in row)])),lineterminator='\r\n');writer.writeheader();writer.writerows(rows)
   return buffer.getvalue().encode('utf-8'),'text/csv','dialogue-edited.csv'
  return json.dumps(root,ensure_ascii=False,indent=2).encode(),'application/json','corpus-edited.json'
 def close(self):
  # Release the SQLite file handle explicitly, including on Windows.
  with self.lock:
   if not getattr(self,'closed',False):self.db.close();self.cache.clear();self.staged.clear();self.closed=True
 def setting(self,key,default=None):
  with self.lock:return read_setting(self.db,key,default)
 def set_setting(self,key,value):
  with self.lock:write_setting(self.db,key,value)
 def add_asset(self,metadata):
  self.add_assets([metadata])
  return metadata
 def add_assets(self,assets):
  rows=[]
  for metadata in assets:
   ident=metadata.get('id') or uuid.uuid4().hex
   metadata['id']=ident
   rows.append((ident,json.dumps(metadata)))
  with self.lock,self.db:
   self.db.executemany('insert or replace into assets values(?,?)',rows)
 def assets(self,newest_first=False):
  with self.lock:return [json.loads(row[0]) for row in self.db.execute('select metadata from assets order by rowid '+('desc' if newest_first else 'asc'))]
 def pieces_for_line(self,line_id):
  # Narrow valid JSON through the index. Older Python-serialized nonfinite
  # metadata remains eligible for the unchanged compatibility filters below.
  query="""select rowid as asset_order,metadata from assets
where json_valid(metadata) and json_extract(metadata,'$.kind')='piece'
and json_extract(metadata,'$.line_id')=?
union all select rowid as asset_order,metadata from assets indexed by assets_non_json_lookup where not json_valid(metadata)
order by asset_order desc"""
  with self.lock:return [json.loads(row[1]) for row in self.db.execute(query,(line_id,))]
 def asset(self,ident):
  with self.lock:row=self.db.execute('select metadata from assets where id=?',(ident,)).fetchone()
  if not row:raise ValueError('Unknown audio asset.')
  return json.loads(row[0])
 def add_take(self,metadata):
  ident=metadata.get('id') or uuid.uuid4().hex;metadata['id']=ident
  with self.lock:self.db.execute('insert or replace into takes values(?,?)',(ident,json.dumps(metadata)));self.db.commit()
  return metadata
 def takes(self,record_id):
  query="""select rowid as take_order,metadata from takes
where json_valid(metadata) and json_extract(metadata,'$.record_id') is ?
union all select rowid as take_order,metadata from takes indexed by takes_non_json_lookup where not json_valid(metadata)
order by take_order desc"""
  with self.lock:rows=self.db.execute(query,(record_id,)).fetchall()
  return [t for row in rows if (t:=json.loads(row[1])).get('record_id')==record_id]
