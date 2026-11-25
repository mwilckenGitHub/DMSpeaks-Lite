"""Disk-backed library metadata. Audio is opened only by resolved playback."""
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unicodedata

from engine_limits import MAX_SEGMENTS
from library_manifest import match_key
from library_stream import FORMAT,VERSION,MAX_RECORD_BYTES,integer,loads,legacy_records,csv_policy_file
from model import token_value

SCHEMA_VERSION='1'


def file_digest(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def source_digest(path):
    path=Path(path)
    digest=file_digest(path)
    if path.suffix.lower()=='.csv':
        digest=hashlib.sha256((digest+file_digest(csv_policy_file(path))).encode()).hexdigest()
    return digest


def connect(path,readonly=False):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro' if readonly else path,uri=readonly)
    db.execute('pragma trusted_schema=off')
    db.execute('pragma cache_size=-4096')
    db.execute('pragma temp_store=file')
    return db


def metadata(db):
    return dict(db.execute('select key,value from meta'))


def completed(path,digest):
    if not Path(path).is_file():return False
    try:
        with closing(connect(path,True)) as db:
            meta=metadata(db)
            return meta.get('complete')=='1' and meta.get('source_sha256')==digest and meta.get('schema_version')==SCHEMA_VERSION
    except sqlite3.DatabaseError:return False


def _put(db,record,repeated=False):
    kind=record.get('record')
    if kind=='header':
        if record.get('format')!=FORMAT or record.get('version')!=VERSION:raise ValueError('Unsupported streamed library format/version.')
        if db.execute("select 1 from meta where key='header'").fetchone():raise ValueError('Duplicate streamed header.')
        db.execute('insert into meta values(?,?)',('header',json.dumps(record,ensure_ascii=False)))
        return
    key=record.get('key')
    if not isinstance(key,str) or not key or len(key)>4096:raise ValueError('Invalid streamed dialogue key.')
    prior=db.execute('select id from dialogues where external_key=?',(key,)).fetchone()
    if kind=='dialogue':
        entry=record.get('entry')
        if not isinstance(entry,dict) or 'segments' in entry:raise ValueError('A streamed dialogue descriptor must omit segments.')
        policy=entry.get('assembly',{})
        if not isinstance(policy,dict) or policy.get('format')!='dmspeaks-clip-sequence' or policy.get('version')!='0.1':raise ValueError('Unsupported sequence policy.')
        payload=json.dumps(entry,ensure_ascii=False,sort_keys=True)
        if prior:
            if not repeated or db.execute('select payload from dialogues where id=?',(prior[0],)).fetchone()[0]!=payload:raise ValueError('Duplicate or conflicting dialogue descriptor.')
            return
        label=str(entry.get('dialogue_id',key))
        db.execute('insert into dialogues(external_key,label,search_key,payload) values(?,?,?,?)',
            (key,label,unicodedata.normalize('NFC',label).casefold(),payload))
        return
    if not prior:raise ValueError('A streamed record precedes its dialogue descriptor.')
    ident=prior[0]
    if kind=='segment':
        segment=record.get('segment')
        if not isinstance(segment,dict) or 'clips' in segment:raise ValueError('A streamed segment descriptor must omit clips.')
        order=integer(segment.get('order'),'segment order',1,MAX_SEGMENTS)
        if not all(isinstance(segment.get(k),str) for k in ['type','placeholder','layer_id','track_id']):raise ValueError('Invalid segment identity.')
        match_key('',segment.get('value_match','unicode_nfc'))
        payload=json.dumps(segment,ensure_ascii=False,sort_keys=True)
        previous=db.execute('select payload from segments where dialogue=? and ordinal=?',(ident,order)).fetchone()
        if previous:
            if not repeated or previous[0]!=payload:raise ValueError('Duplicate or conflicting occurrence descriptor.')
            return
        db.execute('insert into segments values(?,?,?)',(ident,order,payload))
    elif kind=='clip':
        order=integer(record.get('order'),'segment order',1,MAX_SEGMENTS)
        row=db.execute('select payload from segments where dialogue=? and ordinal=?',(ident,order)).fetchone()
        if not row:raise ValueError('A candidate precedes its occurrence descriptor.')
        segment=loads(row[0]);clip=record.get('clip')
        if not isinstance(clip,dict) or clip.get('export_status')!='available':raise ValueError('Streamed candidates must be available delivery clips.')
        context=[clip.get('clip_variant','default'),clip.get('clip_reference_id',''),clip.get('clip_generation_key','')]
        if not all(isinstance(value,str) for value in context):raise ValueError('Invalid candidate selection context.')
        value=match_key(clip.get('player_value'),segment.get('value_match','unicode_nfc'))
        db.execute('insert into clips(dialogue,ordinal,value_key,variant,reference,generation,payload) values(?,?,?,?,?,?,?)',
            (ident,order,value,*context,json.dumps(clip,ensure_ascii=False)))
    else:raise ValueError('Unknown streamed library record.')


def build_index(source,target,status=lambda _:None,cancelled=lambda:False,digest=None):
    """Atomically publish a complete index. JSONL imports resume at checkpoints.

    The guard is an SQLite lock, released by the OS if the process exits. Source
    identity is a full SHA-256; cache files never live inside an exported library.
    """
    source=Path(source).resolve();target=Path(target).resolve()
    if source.parent in target.parents:raise ValueError('Keep the index outside the exported library folder.')
    target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    digest=digest or source_digest(source);partial=Path(str(target)+'.partial')
    guard=sqlite3.connect(str(target)+'.lock',timeout=1)
    db=None
    try:
        guard.execute('begin exclusive')
        if completed(target,digest):return target
        resumable=source.suffix.lower()=='.jsonl'
        if partial.exists():
            try:
                with closing(connect(partial,True)) as check:old=metadata(check)
                resume=resumable and old.get('source_sha256')==digest and old.get('schema_version')==SCHEMA_VERSION
            except sqlite3.DatabaseError:resume=False
            if not resume:partial.unlink()
        if not partial.exists():partial.touch(mode=0o600)
        db=connect(partial)
        db.executescript('''create table if not exists meta(key text primary key,value text);
            create table if not exists dialogues(id integer primary key,external_key text unique,label text,search_key text,payload text);
            create table if not exists segments(dialogue integer,ordinal integer,payload text,primary key(dialogue,ordinal));
            create table if not exists clips(id integer primary key,dialogue integer,ordinal integer,value_key text,variant text,reference text,generation text,payload text);''')
        meta=metadata(db);offset=int(meta.get('offset','0'));records=int(meta.get('records','0'))
        for key,value in [('source_sha256',digest),('schema_version',SCHEMA_VERSION)]:db.execute('insert or replace into meta values(?,?)',(key,value))
        db.commit();ended=meta.get('import_complete')=='1'

        def accept(record,position):
            nonlocal records,ended
            if not isinstance(record,dict):raise ValueError('Manifest records must be objects.')
            if ended:raise ValueError('Unexpected records after the streamed end marker.')
            if records==0 and record.get('record')!='header':raise ValueError('The first record must be a streamed header.')
            if record.get('record')=='end':
                for field,table in [('dialogues','dialogues'),('clips','clips')]:
                    actual=db.execute('select count(*) from '+table).fetchone()[0]
                    if resumable and field not in record:raise ValueError('Streamed terminal counts are required.')
                    if field in record and integer(record[field],field)!=actual:raise ValueError('Streamed terminal counts do not match '+field+'.')
                ended=True
            else:_put(db,record,repeated=source.suffix.lower()=='.csv')
            records+=1
            if records%2000==0 and not ended:
                db.execute('insert or replace into meta values(?,?)',('offset',str(position)))
                db.execute('insert or replace into meta values(?,?)',('records',str(records)))
                db.commit();status(dict(records=records,bytes=position,total_bytes=source.stat().st_size))
            if cancelled():raise InterruptedError('Library indexing interrupted; JSONL checkpoint retained.')

        if not ended and resumable:
            with source.open('rb') as stream:
                stream.seek(offset)
                while True:
                    line=stream.readline(MAX_RECORD_BYTES+1)
                    if not line:break
                    if len(line)>MAX_RECORD_BYTES:raise ValueError('A streamed manifest record exceeds 2 MiB.')
                    if not line.endswith(b'\n'):raise ValueError('Incomplete streamed manifest line; export did not finish.')
                    accept(loads(line.decode('utf-8-sig' if stream.tell()==len(line) else 'utf-8')),stream.tell())
        elif not ended:
            for record in legacy_records(source):accept(record,0)
        if not ended:raise ValueError('The streamed library has no end marker; export is incomplete.')
        total=db.execute('select count(*) from dialogues').fetchone()[0]
        if not total:raise ValueError('The library needs at least one dialogue.')
        invalid=db.execute('''select d.id from dialogues d left join segments s on d.id=s.dialogue
            group by d.id having count(s.ordinal)=0 or min(s.ordinal)!=1 or max(s.ordinal)!=count(s.ordinal) limit 1''').fetchone()
        if invalid:raise ValueError('Segment orders must be consecutive from 1 in every dialogue.')
        if source_digest(source)!=digest:raise ValueError('The manifest or sequence policy changed during indexing. Retry the current file.')
        db.execute('insert or replace into meta values(?,?)',('import_complete','1'))
        db.execute('insert or replace into meta values(?,?)',('records',str(records)));db.commit()
        db.executescript('''create index if not exists dialogue_search on dialogues(search_key,id);
            create index if not exists clip_value on clips(dialogue,ordinal,variant,value_key,id);
            create index if not exists clip_value_context on clips(dialogue,ordinal,variant,value_key,reference,generation,id);
            create index if not exists clip_priority on clips(dialogue,ordinal,variant,id);
            create index if not exists clip_context on clips(dialogue,ordinal,variant,reference,generation,id);''')
        if source_digest(source)!=digest:raise ValueError('The manifest changed during indexing. Retry the current file.')
        db.execute('insert or replace into meta values(?,?)',('complete','1'));db.commit();db.close();db=None
        os.replace(partial,target)
        status(dict(complete=True,dialogues=total,records=records))
        return target
    finally:
        if db is not None:db.close()
        guard.close()


def page_cursor(query,index):
    return base64.urlsafe_b64encode(json.dumps([query,index],ensure_ascii=False).encode()).decode()


def read_cursor(cursor,query):
    if cursor is None:return 0
    if not isinstance(cursor,str) or len(cursor)>4096:raise ValueError('Invalid catalog page cursor.')
    try:
        q,index=loads(base64.b64decode(cursor,altchars=b'-_',validate=True))
        if q!=query:raise ValueError()
        return integer(index,'page cursor',1)
    except (ValueError,TypeError,UnicodeError):raise ValueError('Catalog cursor belongs to another search or is invalid.') from None


class CatalogIndex:
    def __init__(self,source,cache_dir=None,status=lambda _:None):
        self.temporary=None
        if cache_dir is None:
            self.temporary=tempfile.TemporaryDirectory(prefix='dmspeaks-library-');cache_dir=self.temporary.name
        digest=source_digest(source)
        self.identity=digest
        self.path=build_index(source,Path(cache_dir)/(digest+'.sqlite'),status,digest=digest)
        with closing(connect(self.path,True)) as db:self.count=db.execute('select count(*) from dialogues').fetchone()[0]

    def close(self):
        if self.temporary is not None:self.temporary.cleanup();self.temporary=None

    def page(self,query='',cursor=None,limit=50):
        if not isinstance(query,str) or len(query)>512:raise ValueError('Search prefixes are limited to 512 characters.')
        limit=integer(limit,'catalog page size',1,100);after=read_cursor(cursor,query)
        search=unicodedata.normalize('NFC',query).casefold()
        with closing(connect(self.path,True)) as db:
            if search:
                # Range comparison preserves literal %, _ and backslash, unlike LIKE.
                upper=None
                for position in range(len(search)-1,-1,-1):
                    if ord(search[position])<0x10ffff:
                        upper=search[:position]+chr(ord(search[position])+1);break
                clauses='search_key>=?';parameters=[search]
                if upper is not None:clauses+=' and search_key<?';parameters.append(upper)
                matched=db.execute('select count(*) from dialogues where '+clauses,parameters).fetchone()[0]
                if after:
                    previous=db.execute('select search_key from dialogues where id=?',(after,)).fetchone()
                    if not previous or not previous[0].startswith(search):raise ValueError('Invalid catalog page cursor.')
                    clauses+=' and (search_key,id)>(?,?)';parameters.extend([previous[0],after])
                rows=db.execute('select id,label from dialogues where '+clauses+' order by search_key,id limit ?',[*parameters,limit+1]).fetchall()
            else:
                matched=self.count
                rows=db.execute('select id,label from dialogues where id>? order by id limit ?',(after,limit+1)).fetchall()
            items=[]
            for ident,label in rows[:limit]:
                occurrences=[];pieces=[]
                for payload, in db.execute('select payload from segments where dialogue=? order by ordinal',(ident,)):
                    segment=loads(payload)
                    pieces.append({key:segment.get(key) for key in ['order','placeholder','type']})
                    if segment['type']!='fixed':occurrences.append({key:segment.get(key) for key in ['order','placeholder','type']})
                items.append(dict(index=ident-1,id=label,occurrences=occurrences,pieces=pieces))
        return dict(items=items,total=self.count,matched=matched,next_cursor=page_cursor(query,rows[limit-1][0]) if len(rows)>limit else None)

    def _where(self,ident,segment,player,variant,reference,generation):
        grouped=reference is not None and generation is not None
        lookup=('clip_context' if grouped else 'clip_priority') if segment['type']=='fixed' else ('clip_value_context' if grouped else 'clip_value')
        sql=' from clips indexed by '+lookup+' where dialogue=? and ordinal=? and variant=?'
        parameters=[ident,segment['order'],variant]
        if segment['type']!='fixed':
            value=token_value(segment['placeholder'],player)[1]
            if value in ('',None):sql+=' and 0'
            sql+=' and value_key=?';parameters.append(match_key(value,segment.get('value_match','unicode_nfc')))
        if reference is not None:sql+=' and reference=?';parameters.append(reference)
        if generation is not None:sql+=' and generation=?';parameters.append(generation)
        return sql,parameters

    def _scope(self,index,segment,player,variant,reference,generation):
        value=None if segment['type']=='fixed' else token_value(segment['placeholder'],player)[1]
        if value is not None:value=match_key(value,segment.get('value_match','unicode_nfc'))
        return hashlib.sha256(json.dumps([self.identity,index,segment['order'],variant,reference,generation,value],ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def selections(value):
        if not isinstance(value,dict) or len(value)>MAX_SEGMENTS:raise ValueError('Recording selections must be an occurrence-to-token object.')
        for order,token in value.items():
            if not isinstance(order,str) or not order.isascii() or not order.isdecimal() or str(int(order))!=order or not 1<=int(order)<=MAX_SEGMENTS or not isinstance(token,str) or not token or len(token)>4096:
                raise ValueError('Invalid recording selection.')
        return value

    def _entry(self,db,index,player,variant,reference_id,generation_key,selections):
        """Preselect through indexed SQL, preserving the reference resolver priority."""
        ident=integer(index,'entry index')+1
        selections=self.selections(selections)
        row=db.execute('select payload from dialogues where id=?',(ident,)).fetchone()
        if not row:raise ValueError('Unknown dialogue entry.')
        entry=loads(row[0]);segments=[loads(row[0]) for row in db.execute('select payload from segments where dialogue=? order by ordinal',(ident,))]
        if set(selections)-{str(s['order']) for s in segments}:raise ValueError('Unknown recording occurrence.')
        def first(segment,reference,generation):
            sql,parameters=self._where(ident,segment,player,variant,reference,generation)
            result=db.execute('select payload'+sql+' order by id limit 1',parameters).fetchone()
            return loads(result[0]) if result else None
        cached={}
        # Establish the exported group BEFORE applying any occurrence overrides.
        for segment in sorted(segments,key=lambda s:s['type']!='fixed'):
            chosen=first(segment,reference_id,generation_key)
            if chosen:
                cached[segment['order']]=chosen
                if reference_id is None:reference_id=chosen.get('clip_reference_id','')
                if generation_key is None:generation_key=chosen.get('clip_generation_key','')
                break
        for segment in segments:
            token=selections.get(str(segment['order']))
            if token:
                candidate=read_cursor(token,self._scope(index,segment,player,variant,reference_id,generation_key))
                sql,parameters=self._where(ident,segment,player,variant,reference_id,generation_key)
                row=db.execute('select payload'+sql+' and id=?',parameters+[candidate]).fetchone()
                if not row:raise ValueError('The selected recording is unavailable for this occurrence and voice group.')
                chosen=loads(row[0])
            else:chosen=cached.get(segment['order']) or first(segment,reference_id,generation_key)
            segment['clips']=[chosen] if chosen else []
        entry['segments']=segments
        return entry,reference_id,generation_key

    def entry(self,index,player,variant='default',reference_id=None,generation_key=None,selections=None):
        with closing(connect(self.path,True)) as db:
            return self._entry(db,index,player,variant,reference_id,generation_key,{} if selections is None else selections)[0]

    def candidates(self,index,player,order,variant='default',reference_id=None,generation_key=None,selections=None,cursor=None,limit=50):
        """Bounded, occurrence-scoped choices; no audio files are read or authorized."""
        index=integer(index,'entry index')
        order=integer(order,'recording occurrence',1,MAX_SEGMENTS)
        limit=integer(limit,'recording page size',1,100)
        selections=self.selections({} if selections is None else selections)
        with closing(connect(self.path,True)) as db:
            entry,reference,generation=self._entry(db,index,player,variant,reference_id,generation_key,selections)
            segment=next((s for s in entry['segments'] if s['order']==order),None)
            if segment is None:raise ValueError('Unknown recording occurrence.')
            scope=self._scope(index,segment,player,variant,reference,generation)
            after=read_cursor(cursor,scope)
            sql,parameters=self._where(index+1,segment,player,variant,reference,generation)
            total=db.execute('select count(*)'+sql,parameters).fetchone()[0]
            rows=db.execute('select id,payload'+sql+' and id>? order by id limit ?',parameters+[after,limit+1]).fetchall()
            def summary(ident,clip):
                frames=clip.get('audio_sample_count');rate=clip.get('audio_sample_rate')
                return dict(token=page_cursor(scope,ident),clip_id=str(clip.get('clip_id',''))[:256],
                    seconds=frames/rate if type(frames) is int and frames>0 and type(rate) is int and 8000<=rate<=192000 else None)
            first=db.execute('select id,payload'+sql+' order by id limit 1',parameters).fetchone()
            selected=selections.get(str(order))
            selected_id=read_cursor(selected,scope) if selected else first[0] if first else None
            selected_clip=segment['clips'][0] if segment['clips'] else None
            return dict(index=index,order=order,selection_context=dict(variant=variant,reference_id=reference,generation_key=generation),
                items=[summary(ident,loads(payload)) for ident,payload in rows[:limit]],total=total,
                selected=summary(selected_id,selected_clip) if selected_clip else None,
                next_cursor=page_cursor(scope,rows[limit-1][0]) if len(rows)>limit else None)
