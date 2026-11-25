"""Bounded records for portable, provider-free library interchange."""
import copy
import csv
import json
from pathlib import Path

from engine_limits import MAX_SEGMENTS

FORMAT='dmspeaks-streamed-audio-library'
VERSION='0.1'
MAX_RECORD_BYTES=2*1024*1024
MAX_JSON_BYTES=32*1024*1024
SEQUENCING=('anchor_start_sample','gap_samples','overlap_samples','fade_in_samples',
            'fade_out_samples','fallback_silence_ms')


def integer(value,field,lower=0,upper=2**53-1):
    if isinstance(value,str) and value.isdecimal():value=int(value)
    if type(value) is not int or not lower<=value<=upper:raise ValueError('Invalid '+field+'.')
    return value


def loads(raw):
    return json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite manifest values are unsupported.')))


def write_record(stream,record):
    encoded=json.dumps(record,ensure_ascii=False,allow_nan=False,separators=(',',':'))+'\n'
    if len(encoded.encode('utf-8'))>MAX_RECORD_BYTES:raise ValueError('A streamed manifest record exceeds 2 MiB.')
    stream.write(encoded)


def dialogue_key(entry):
    return json.dumps([entry.get('corpus_id',''),entry.get('dialogue_index',entry.get('dialogue_id'))],ensure_ascii=False,separators=(',',':'))


def csv_policy_file(source):
    path=Path(source).parent/'sequence-policy.json'
    if not path.resolve().is_relative_to(Path(source).parent.resolve()):raise ValueError('Sequence policy must stay inside the exported library.')
    if path.stat().st_size>MAX_JSON_BYTES:raise ValueError('Sequence policy exceeds 32 MiB.')
    return path


def csv_rows(path):
    """Stream rows without materializing a whole dialogue or occurrence."""
    # Python's default CSV field limit is smaller than a permitted record.
    # Never lower it: other threads may also be reading library CSV files.
    if csv.field_size_limit()<MAX_RECORD_BYTES:csv.field_size_limit(MAX_RECORD_BYTES)
    with Path(path).open(encoding='utf-8-sig',newline='') as stream:
        class RecordLines:
            used=0
            def __iter__(self):return self
            def __next__(self):
                # Bound a whole quoted logical record, not only each field.
                line=stream.readline(MAX_RECORD_BYTES-self.used+1)
                if not line:raise StopIteration
                self.used+=len(line.encode('utf-8'))
                if self.used>MAX_RECORD_BYTES:raise ValueError('A manifest CSV row exceeds 2 MiB.')
                return line
        lines=RecordLines();reader=csv.DictReader(lines,strict=True)
        required={'corpus_id','dialogue_index','dialogue_id','segment_order','segment_type',
            'segment_placeholder','segment_layer_id','segment_track_id','audio_path','export_status',
            'audio_sha256','clip_id','player_value','audio_sample_count','audio_sample_rate','audio_channels'}
        if not required<=set(reader.fieldnames or []):raise ValueError('CSV is not a DMSpeaks exported audio-library manifest.')
        while True:
            lines.used=0
            try:row=next(reader)
            except StopIteration:break
            if None in row or any(value is None for value in row.values()):raise ValueError('Malformed manifest CSV row.')
            if sum(len(value.encode('utf-8')) for value in row.values())>MAX_RECORD_BYTES:raise ValueError('A manifest CSV row exceeds 2 MiB.')
            yield row


def csv_parts(row,policy):
    entry=dict(corpus_id=row['corpus_id'],dialogue_index=row['dialogue_index'],dialogue_id=row['dialogue_id'],assembly=copy.deepcopy(policy))
    order=integer(row['segment_order'],'segment order',1,MAX_SEGMENTS)
    segment=dict(order=order,type=row['segment_type'],placeholder=row['segment_placeholder'],
        layer_id=row['segment_layer_id'],track_id=row['segment_track_id'],
        after_layer_id=row.get('segment_after_layer_id') or None,
        missing_policy=row.get('segment_missing_policy') or 'error',value_match=row.get('segment_value_match') or 'unicode_nfc')
    segment.update({key:integer(row.get('segment_'+key) or '0',key) for key in SEQUENCING})
    clip=None
    if row['export_status']=='available':
        clip=dict(export_status='available',audio_path=row['audio_path'],audio_sha256=row['audio_sha256'],
            clip_id=row['clip_id'],player_value=row['player_value'],clip_variant=row.get('clip_variant') or 'default',
            clip_reference_id=row.get('clip_reference_id') or '',clip_generation_key=row.get('clip_generation_key') or '',
            audio_sample_count=integer(row['audio_sample_count'],'audio sample count',1),
            audio_sample_rate=integer(row['audio_sample_rate'],'audio sample rate',8000,192000),
            audio_channels=integer(row['audio_channels'],'audio channels',1,2))
    return dialogue_key(entry),entry,segment,clip


def legacy_records(source):
    """Convert bounded JSON or streamed CSV to the same indexing records."""
    source=Path(source)
    if source.suffix.lower()=='.csv':
        policy_path=csv_policy_file(source)
        policy=loads(policy_path.read_bytes().decode('utf-8-sig'))
        yield dict(record='header',format=FORMAT,version=VERSION)
        for row in csv_rows(source):
            key,entry,segment,clip=csv_parts(row,policy)
            # Repeated descriptors are checked for equality by the CSV importer.
            yield dict(record='dialogue',key=key,entry=entry)
            yield dict(record='segment',key=key,segment=segment)
            if clip:yield dict(record='clip',key=key,order=segment['order'],clip=clip)
    else:
        with source.open('rb') as stream:raw=stream.read(MAX_JSON_BYTES+1)
        if len(raw)>MAX_JSON_BYTES:raise ValueError('Legacy JSON exceeds 32 MiB. Export streamed JSONL or use CSV.')
        data=loads(raw.decode('utf-8-sig'))
        if not isinstance(data,dict) or data.get('format')!='voxweave-audio-library' or data.get('version')!='0.1':raise ValueError('Unsupported audio-library manifest.')
        if not isinstance(data.get('dialogues'),list):raise ValueError('Invalid dialogue catalog.')
        yield dict(record='header',format=FORMAT,version=VERSION,corpus=data.get('corpus',{}))
        for number,entry in enumerate(data['dialogues']):
            if not isinstance(entry,dict):raise ValueError('Invalid dialogue descriptor.')
            key=str(number)
            yield dict(record='dialogue',key=key,entry={k:v for k,v in entry.items() if k!='segments'})
            for segment in entry.get('segments',[]):
                yield dict(record='segment',key=key,segment={k:v for k,v in segment.items() if k!='clips'})
                for clip in segment.get('clips',[]):
                    if clip.get('export_status')=='available':yield dict(record='clip',key=key,order=segment['order'],clip=clip)
    yield dict(record='end')
