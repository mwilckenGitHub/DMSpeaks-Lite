"""Indexed occurrence queries over immutable asset metadata.

The projection is disposable. SQLite triggers keep direct Store writes in sync;
source metadata is never rewritten. Media gates are evaluated on each query,
once per distinct path/review policy rather than once per aliasing candidate.
"""
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import uuid

from content_review import is_rejected,is_delivery_eligible,requires_isolation_review,media_digest

VERSION='4'
PAGE_SIZE=50


def canonical_key(value):
    """Python equality for finite JSON values, with exact numeric ratios.

    Local Qwen supplied a recursive outline; its bool/fraction handling failed
    its own stated contract and was replaced before integration.
    """
    def encode(item):
        if item is None:return ['null']
        if isinstance(item,str):return ['text',item]
        if isinstance(item,(bool,int,float)):
            if isinstance(item,float):
                if not math.isfinite(item):raise ValueError()
                numerator,denominator=item.as_integer_ratio()
            else:numerator,denominator=int(item),1
            return ['number',numerator,denominator]
        if isinstance(item,list):return ['list',[encode(v) for v in item]]
        if isinstance(item,dict) and all(isinstance(k,str) for k in item):
            return ['dict',[[k,encode(v)] for k,v in sorted(item.items())]]
        raise ValueError()
    try:return json.dumps(encode(value),ensure_ascii=False,separators=(',',':'),allow_nan=False)
    except (ValueError,TypeError):return None


def digest(value):return hashlib.sha256(value.encode('utf-8')).hexdigest()


def base_key(record,segment,variant):
    key=canonical_key([record['id'],segment['order'],record['metadata'].get('npc_voice_profile'),
        record['revision'],variant,segment.get('segment_model','legacy'),segment.get('literal_suffix','')])
    return digest(key) if key is not None else None


def candidate_search_text(asset):
    return str(asset.get('value', '')).casefold()


def projection(raw):
    try:
        asset=json.loads(raw)
        if not isinstance(asset,dict) or asset.get('kind')!='piece':return None
        base=canonical_key([asset.get('line_id'),asset.get('slot_order'),asset.get('voice'),
            asset.get('source_revision'),asset.get('variant','default'),
            asset.get('segment_model','legacy'),asset.get('literal_suffix','')])
        review=asset.get('content_review') or {}
        review={k:review.get(k) for k in ['status','provenance','media_sha256','isolated_content','complete_pronunciation']} if isinstance(review,dict) else {}
        derivation=asset.get('delivery_derivation') or {}
        derivation={k:derivation.get(k) for k in ['format','reviewed_master_sha256','source_sha256','audio_sha256']} if isinstance(derivation,dict) else {}
        processing=asset.get('clip_processing') or {}
        gate=dict(kind='piece',path=asset.get('path'),content_review=review,
            isolation_review_required=requires_isolation_review(asset),audition_only=bool(asset.get('audition_only')),
            delivery_derivation=derivation,clip_processing={'source_sha256':processing.get('source_sha256')} if isinstance(processing,dict) else {})
        gate_text=json.dumps(gate,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        summary={k:asset[k] for k in ['id','kind','value','seconds','sample_count','sample_rate','channels',
            'variant','reference_id','generation_key','source_revision','source_metadata_revision','dataset','line_id',
            'slot_order','voice','segment_model','literal_suffix','performance_model','record_id'] if k in asset}
        if asset.get('content_review'):summary['content_review']=review
        result=dict(base=digest(base) if base is not None else None,line=canonical_key(asset.get('line_id')),
            dataset=canonical_key(asset.get('dataset')),owner=canonical_key(asset.get('record_id')),metadata_revision=canonical_key(asset.get('source_metadata_revision')),
            generation=canonical_key(asset.get('generation_key')),reference=canonical_key(asset.get('reference_id','')),
            performance=canonical_key(asset.get('performance_model','')),npc=str(asset.get('npc_id') or ''),
            value=canonical_key(asset.get('value')),search=candidate_search_text(asset),
            gate=digest(gate_text),gate_payload=gate,summary=summary,metadata_sha256=digest(raw))
        return json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    except (ValueError,TypeError,AttributeError):
        # Nonfinite unrelated metadata is deliberately omitted from projections;
        # nonfinite selection identities cannot be matched to finite UI values.
        return None


def attach(store):
    db=store.db;db.create_function('ds_candidate_projection',1,projection,deterministic=True)
    db.execute('pragma cache_size=-4096');db.execute('pragma temp_store=file')
    db.executescript('''create table if not exists authoring_candidates(id text primary key,asset_order integer,projection text);
      create table if not exists authoring_gates(id text primary key,payload text);
      create table if not exists authoring_epochs(line text primary key,revision integer not null);
      create table if not exists authoring_schema(version text);''')
    previous=db.execute('select version from authoring_schema').fetchone()
    refresh=previous!=(VERSION,)
    migrate=previous != (VERSION,)
    fields={'base':'base','dataset':'dataset','owner':'owner','metadata_revision':'metadata_revision','generation':'generation',
        'performance':'performance','reference_key':'reference','npc':'npc','value_key':'value','search':'search',
        'gate':'gate','line':'line','summary':'summary','metadata_sha256':'metadata_sha256'}
    if refresh:
        for name in ['authoring_asset_insert','authoring_asset_update','authoring_asset_delete',
                     'authoring_projection_insert','authoring_projection_update','authoring_projection_delete',
                     'authoring_normalize_insert','authoring_normalize_update']:
            db.execute('drop trigger if exists '+name)
    if migrate:
        for name in ['authoring_base_order','authoring_base_value','authoring_base_gate','authoring_scope','authoring_value_scope']:
            db.execute('drop index if exists '+name)
        columns={row[1] for row in db.execute('pragma table_info(authoring_candidates)')}
        for name in fields:
            if name not in columns:db.execute('alter table authoring_candidates add column '+name+' text')
    db.executescript('''
      create index if not exists authoring_base_order on authoring_candidates(base,asset_order desc);
      create index if not exists authoring_base_value on authoring_candidates(base,value_key,asset_order desc);
      create index if not exists authoring_scope on authoring_candidates(base,dataset,metadata_revision,generation,performance,reference_key,npc,value_key,search,gate,asset_order desc,id);
      create index if not exists authoring_value_scope on authoring_candidates(base,value_key,dataset,metadata_revision,generation,performance,reference_key,npc,search,gate,asset_order desc,id);
      create trigger if not exists authoring_asset_insert after insert on assets begin
        insert into authoring_candidates(id,asset_order,projection) values(new.id,new.rowid,ds_candidate_projection(new.metadata))
        on conflict(id) do update set asset_order=excluded.asset_order,projection=excluded.projection;
      end;
      create trigger if not exists authoring_asset_update after update of metadata on assets begin
        insert into authoring_candidates(id,asset_order,projection) values(new.id,new.rowid,ds_candidate_projection(new.metadata))
        on conflict(id) do update set asset_order=excluded.asset_order,projection=excluded.projection;
      end;
      create trigger if not exists authoring_asset_delete after delete on assets begin
        delete from authoring_candidates where id=old.id;
      end;
      create trigger if not exists authoring_projection_insert after insert on authoring_candidates when new.projection is not null begin
        insert into authoring_gates values(json_extract(new.projection,'$.gate'),json_extract(new.projection,'$.gate_payload')) on conflict(id) do nothing;
        insert into authoring_epochs values(json_extract(new.projection,'$.line'),1) on conflict(line) do update set revision=revision+1;
      end;
      create trigger if not exists authoring_projection_update after update of projection,asset_order on authoring_candidates begin
        insert into authoring_gates select json_extract(new.projection,'$.gate'),json_extract(new.projection,'$.gate_payload') where new.projection is not null on conflict(id) do nothing;
        insert into authoring_epochs select json_extract(old.projection,'$.line'),1 where old.projection is not null on conflict(line) do update set revision=revision+1;
        insert into authoring_epochs select json_extract(new.projection,'$.line'),1 where new.projection is not null and (old.projection is null or json_extract(old.projection,'$.line')!=json_extract(new.projection,'$.line')) on conflict(line) do update set revision=revision+1;
      end;
      create trigger if not exists authoring_projection_delete after delete on authoring_candidates when old.projection is not null begin
        insert into authoring_epochs values(json_extract(old.projection,'$.line'),1) on conflict(line) do update set revision=revision+1;
      end;''')
    setters=','.join(name+"=json_extract(new.projection,'$."+key+"')" for name,key in fields.items())
    for name,event in [('insert','insert'),('update','update of projection,asset_order')]:
        db.execute('create trigger if not exists authoring_normalize_'+name+' after '+event+
            ' on authoring_candidates begin update authoring_candidates set '+setters+' where id=new.id; end;')
    if migrate:
        db.execute('delete from authoring_candidates');db.execute('delete from authoring_gates');db.execute('delete from authoring_epochs');db.commit()
        last=0
        while True:
            end=db.execute('select max(rowid) from (select rowid from assets where rowid>? order by rowid limit 500)',(last,)).fetchone()[0]
            if end is None:break
            db.execute('insert or replace into authoring_candidates(id,asset_order,projection) select id,rowid,ds_candidate_projection(metadata) from assets where rowid>? and rowid<=?',(last,end));db.commit();last=end
    if refresh:
        db.execute('delete from authoring_schema');db.execute('insert into authoring_schema values(?)',(VERSION,));db.commit()


class CandidateQuery:
    def __init__(self,store,record,segment,variant='default',reference='',generation='',*,track=False,inspection=False,any_value=False,require_path=False):
        self.store=store;self.line=canonical_key(record['id']);self.inspection=inspection;self.require_path=require_path
        self.clauses=['c.base=?']
        self.parameters=[base_key(record,segment,variant)]
        def equal(field,value):self.clauses.append('c.'+{'reference':'reference_key','value':'value_key'}.get(field,field)+'=?');self.parameters.append(value)
        owner=canonical_key(f"{record['dataset']}:{record['index']}")
        if record.get('source_id_ambiguous',False):
            equal('owner',owner)
        else:
            self.clauses.append('c.owner in (?,?,?)')
            self.parameters.extend([owner,canonical_key(None),canonical_key('')])
        if track:
            equal('dataset',canonical_key(record['dataset']));equal('metadata_revision',canonical_key(record.get('metadata_revision')));equal('generation',canonical_key(generation))
        else:
            self.clauses.append('c.dataset in (?,?,?,?,?,?)')
            self.parameters.extend([canonical_key(record['dataset']),canonical_key(None),canonical_key(''),canonical_key(False),canonical_key([]),canonical_key({})])
            equal('npc',str(record['metadata'].get('npc_id') or ''))
            if generation:equal('generation',canonical_key(generation))
            if reference:equal('reference',canonical_key(reference))
            if record.get('metadata_changed'):
                equal('metadata_revision',canonical_key(record.get('metadata_revision')))
                self.clauses.append('c.metadata_revision!=?');self.parameters.append(canonical_key(None))
            else:
                self.clauses.append('c.metadata_revision in (?,?)')
                self.parameters.extend([canonical_key(record.get('metadata_revision')),canonical_key(None)])
        if segment.get('performance_model'):equal('performance',canonical_key(segment['performance_model']))
        self.index='authoring_value_scope' if not any_value and segment['type']!='fixed' else 'authoring_scope'
        if not any_value and segment['type']!='fixed':equal('value',canonical_key(segment.get('value')))
        self.identity=digest(json.dumps([self.clauses,self.parameters,inspection,require_path],ensure_ascii=False))

    @contextmanager
    def prepared(self,query='',ids=None,values=None):
        if not isinstance(query,str) or len(query)>512:raise ValueError('Use a search of at most 512 characters.')
        clauses=list(self.clauses);parameters=list(self.parameters)
        if query:clauses.append('instr(c.search,?)>0');parameters.append(query.casefold())
        if ids is not None:
            if not ids:clauses.append('0')
            else:clauses.append('c.id in ('+','.join('?' for _ in ids)+')');parameters.extend(ids)
        if values is not None:
            clauses.append('c.value_key in (select value from json_each(?))')
            parameters.append(json.dumps([canonical_key(value) for value in values],ensure_ascii=False))
        where=' and '.join(clauses);name='ac_live_'+uuid.uuid4().hex
        with self.store.lock:
            db=self.store.db;was_transaction=db.in_transaction
            db.execute('create temp table '+name+'(gate text primary key,ready integer,pending integer,payload text)')
            try:
                lookup='' if ids is not None else ' indexed by '+self.index
                rows=db.execute('select g.id,g.payload from authoring_gates g join (select distinct c.gate from authoring_candidates c'+lookup+' where '+where+') selected on selected.gate=g.id',parameters)
                for gate_id,payload in rows:
                    asset=json.loads(payload)
                    present=not self.require_path or isinstance(asset.get('path'),str) and Path(asset['path']).is_file()
                    rejected=is_rejected(asset);usable=not rejected and is_delivery_eligible(asset)
                    ready=present and not rejected and (self.inspection or usable)
                    pending=present and not rejected and not usable
                    db.execute('insert into '+name+' values(?,?,?,?)',(gate_id,int(ready),int(pending),payload))
                yield db,where,parameters,name
            finally:
                db.execute('drop table '+name)
                if not was_transaction:db.commit()

    def page(self,query='',offset=0,limit=PAGE_SIZE,*,pending=False,full=False,selected=None,with_digest=False,
             preferred_value=None):
        if type(offset) is not int or not 0<=offset<=10_000_000 or type(limit) is not int or not 1<=limit<=100:raise ValueError('Choose a valid candidate page.')
        with self.prepared(query) as (db,where,parameters,table):
            join=' from authoring_candidates c indexed by '+self.index+' join '+table+' g on g.gate=c.gate where '+where+' and g.'+('pending' if pending else 'ready')+'=1'
            total=db.execute('select count(*)'+join,parameters).fetchone()[0]
            # Sort bounded identifiers before loading any full projection blobs.
            selected_rows=db.execute('select c.id'+join+' order by c.asset_order desc limit ? offset ?',[*parameters,limit,offset]).fetchall()
            rows=[db.execute('select c.id,c.summary,g.pending,g.payload from authoring_candidates c join '+table+' g on g.gate=c.gate where c.id=?',row).fetchone() for row in selected_rows]
            if selected and selected!='silence' and not any(row[0]==selected for row in rows):
                selected_join=join.replace(' indexed by '+self.index,'')
                row=db.execute('select c.id,c.summary,g.pending,g.payload'+selected_join+' and c.id=?',[*parameters,selected]).fetchone()
                if row:rows.append(row)
            items=[];digests={}
            for ident,payload,needs_review,gate in rows:
                if full:item=self.store.asset(ident)
                else:item=json.loads(payload)
                if pending:item={k:item.get(k) for k in ['id','value','seconds']}
                elif not full:item={**item,'review_pending':bool(needs_review)}
                if with_digest:
                    path=json.loads(gate)['path']
                    if path not in digests:digests[path]=media_digest({'path':path})
                    item['sha256']=digests[path]
                items.append(item)
            result = dict(candidates=items, total=total, offset=offset, page_size=limit)
            if preferred_value is not None and not any(item.get('value') == preferred_value for item in items):
                row = db.execute('select c.summary,g.pending' + join +
                                 ' and c.value_key=? order by c.asset_order desc limit 1',
                                 [*parameters, canonical_key(preferred_value)]).fetchone()
                if row:
                    result['preferred_candidate'] = {**json.loads(row[0]), 'review_pending': bool(row[1])}
            return result

    def find(self,ident,*,summary=False):
        if not isinstance(ident,str):return None
        with self.prepared(ids=[ident]) as (db,where,parameters,table):
            row=db.execute('select c.id,c.summary from authoring_candidates c join '+table+' g on g.gate=c.gate where '+where+' and g.ready=1',parameters).fetchone()
            return (json.loads(row[1]) if summary else self.store.asset(row[0])) if row else None

    def matching_values(self,values):
        with self.prepared(values=values) as (db,where,parameters,table):
            rows=db.execute('select c.id,c.summary from authoring_candidates c join '+table+
                            ' g on g.gate=c.gate where '+where+' and g.ready=1',parameters)
            return [(ident,json.loads(summary).get('value')) for ident,summary in rows]

    def signature(self):
        result=hashlib.sha256()
        with self.prepared() as (db,where,parameters,table):
            epoch=db.execute('select revision from authoring_epochs where line=?',(self.line,)).fetchone()
            result.update(json.dumps([self.identity,epoch[0] if epoch else 0]).encode())
            for gate,payload in db.execute('select gate,payload from '+table+' where ready=1 order by gate'):
                result.update(gate.encode());result.update(media_digest(json.loads(payload)).encode())
        return result.hexdigest()
