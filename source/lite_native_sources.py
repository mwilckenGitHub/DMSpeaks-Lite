"""Verify native corpus rows and decode their real source, without stock paths."""
from collections.abc import Mapping
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import re

import soundfile as sf
from lite_native_capsule import PortableFixture,unpack
from lite_native_admission import canonical
from lite_portable_io import archive_names_under,cache_bytes,digest

PREFETCH_SHA='de12d752e137580ddd929ad314dc1f97c0da9323340bdb82123d4bb472afd398'


class Prefix(Mapping):
    def __init__(self,members,prefix):self.members=members;self.prefix=prefix
    def __iter__(self):
        return (key.removeprefix(self.prefix) for key in archive_names_under(self.members, self.prefix))
    def __len__(self):return sum(1 for _ in self)
    def __getitem__(self,key):return self.members[self.prefix+key]

    def projected_evidence(self, key):
        projection = getattr(self.members, "projected_evidence", None)
        return projection(self.prefix + key) if projection is not None else None


def source_context(row, members, *, progress=None, should_stop=None):
    pointer=row['native_source']
    if set(pointer)!={'capsule_sha256','ordinal','source_contract_sha256'}:
        raise ValueError('The native source needs its exact capsule and preparation identity.')
    checksum=pointer['capsule_sha256']
    if not isinstance(checksum,str) or not re.fullmatch(r'[0-9a-f]{64}',checksum):
        raise ValueError('The native source capsule identity is invalid.')
    prefix='native-capsules/'+checksum+'/'
    projected=Prefix(members,prefix)
    raw=projected['input.json']
    if digest(raw)!=checksum:raise ValueError('The native source capsule changed.')
    cache=getattr(members,'_native_sources',None)
    if cache is None:
        cache={}
        if hasattr(members,'__dict__'):members._native_sources=cache
    if checksum not in cache:
        manifest, verified, _ = unpack(projected, progress=progress, should_stop=should_stop)
        cache[checksum]=(manifest,PortableFixture(manifest,verified))
    manifest,fixture=cache[checksum]
    choices=[item for item in manifest['rows'] if item['source_contract']['ordinal']==pointer['ordinal']]
    if len(choices)!=1 or digest(canonical(choices[0]['source_contract']))!=pointer['source_contract_sha256']:
        raise ValueError('The native source has a different preparation plan or retained reference.')
    return choices[0],fixture,fixture.entries[pointer['ordinal']]


def derived_row(pointer, members, *, progress=None, should_stop=None):
    admitted, fixture, entry = source_context({'native_source': pointer}, members, progress=progress,
                                                should_stop=should_stop)
    source=admitted['source_contract']
    plan=json.loads(fixture.member(entry['exact_plan']))
    recipe=json.loads(fixture.member(entry['exact_manifest']))
    clock=json.loads(fixture.member(entry['base_alignment']))
    # Earlier admitted capsules use the existing catalog-wide filters.
    refreshed = "630d230ce45dc16830241ef0bf9e6410bc2bf61e187d5a5809b943348e7b5230"
    declared_prefetch = entry.get("eligibility_authority", {}).get("sha256")
    current = "5e1f4c5a5829f867f0cfe97a175bff71db2fc0ffea4c2a758d793edf6083e809"
    next_review = "9b8a51960a45550cdec1ff08657a8cb86f5f83e2222a92dc378fc82719d8b648"
    approved = {refreshed, current, next_review, "a4966d80080c90d3fcdc0ee7e9582be9798cf08eb947fd756ff94871f500dec5", "c4989944270f5103b44811bd82e13470917a9a34ba8ed2a3f18e30be60ef8680", "b4e725908eb0fda044b697f1203fa2a9a10bcc2dba0627d965802d25979e3532"}
    approved.add("a057415aa62934d9d30918706a76e039d4697dfab53186d68886e2534121ce65")
    approved.add("fb1f8ef15997692d1773b6f7ede329bd5cb8460ce424d2ceaabdf537b195b139")
    approved.add("322f5bb69d9f9a049e7d1a1f95588b20d27800487d986e8db1976a60e8013f7d")
    approved.add("9536272ffa90620d372c18b2034497d525f2850c65f8b042c267f50197188329")
    approved.add("4d0d680fd71d68c859f072f71b78736a448910ec4c785ac10d49345bafdacc1e")
    approved.add("bee80520c8b3bfc47801aa6fc4927200b28dca239db402bdedd7e8a1faed747d")
    prefetch_sha = declared_prefetch if declared_prefetch in approved else PREFETCH_SHA
    raw = members["native-prefetch/" + prefetch_sha + ".json"]
    if digest(raw)!=prefetch_sha:raise ValueError('The admitted native prefetch authority changed.')
    evidence=json.loads(raw)
    records=[record for record in evidence['rows'] if record['ordinal']==source['ordinal']]
    if len(records)!=1:raise ValueError('The native source lacks prefetch evidence.')
    restriction=records[0]
    if (restriction['state']!='prefetch-filters-verified'
        or (restriction['source_id'],restriction['npc_id'],restriction['locale'],restriction['original_text_sha256'])
           !=(source['source_id'],source['npc_id'],source['locale'],source['route']['original_text_sha256'])):
        raise ValueError('The native source prefetch filters belong to another corpus owner.')
    filters=restriction['filters']
    addon=next(part for part in Path(entry['exact_manifest']['source_path']).parts
               if part in {'DMSpeaksSpokenAlliance','DMSpeaksSpokenHorde','DMSpeaksSpokenShared','DMSpeaksSpokenGossip'})
    route={**source['route'],'original_text':source['original_text'],
           'giver_type':{'creature':'Creature','gameobject':'GameObject'}[plan['row']['source_metadata']['npcType']]}
    row=dict(native_source=deepcopy(pointer),native_route=route,
             source_id=source['source_id'],npc_id=source['npc_id'],npc=entry['npc'],locale=source['locale'],
             source_text_sha256=source['route']['original_text_sha256'],catalog_ordinal=source['ordinal'],
             key=source['route']['key'],area='rest',races=filters['races'],classes=filters['classes'],
             native_prefetch={'sha256':prefetch_sha,'additional_conditions':filters['additional_conditions']},
             stock_text=plan['stock_text'],base_phones=clock,samples=source['base']['samples'],
             pcm_sha256=source['base']['pcm_sha256'],source_wave_sha256=source['base']['wav_sha256'],
             companion_seed=dict(addon=addon,manifest_sha256=entry['exact_manifest']['sha256']),
             native_delivery_policy=admitted['delivery_policy'])
    if filters['sexes']:row['sexes']=filters['sexes']
    if admitted['reference_state']=='verified':
        row.update(reference_contract=admitted['reference_contract'],request_reference_evidence=admitted['request_reference_evidence'])
    else:row['native_reference_hold']=admitted['reference_hold']
    jobs=[job for job in plan['jobs'] if job['kind']=='name']
    parts=[part for part in recipe['sequence'] if part['kind']=='name']
    slots=[]
    from lite_portable_companion import fallback_context_articles
    for job,part in zip(jobs,parts,strict=True):
        slot=dict(context=job['context'],slot=[job['slot']['start'],job['slot']['end']],
                  source_cut=part['source_samples'],leading_word=part['choices'][part['fallback']].get('leading_word'))
        for kind in ('class', 'race'):
            articles = fallback_context_articles(recipe['sequence'], slot, kind)
            if articles:
                slot[kind + '_articles'] = articles
        slots.append(slot)
    if not slots:raise ValueError('The native source contains no measured Name occurrence.')
    if len(slots)==1:row.update(slots[0])
    else:row['name_slots']=slots
    return row


def verify_row(row, members, *, progress=None, should_stop=None):
    expected = derived_row(row['native_source'], members, progress=progress, should_stop=should_stop)
    prepared = {key: value for key, value in row.items() if key != "paced_base_analysis"}
    if canonical(prepared)!=canonical(expected):
        raise ValueError('The native source row differs from its admitted source, clock, reference or filters.')
    from phoneme_alignment import validate
    from lite_portable_join import expanded
    validate(row['base_phones'],dict(sha256=row['source_wave_sha256'],sample_rate=44100,sample_count=row['samples']),
             expanded(row['stock_text']).strip())


def base_pcm(row,members,output):
    _,fixture,entry=source_context(row,members)
    raw=fixture.member(entry['generated_base'])
    pcm,rate=sf.read(BytesIO(raw),dtype='int16')
    from lite_portable_inputs import sample_checked
    sample_checked(pcm,rate,row)
    cache_bytes(output,raw)
    return pcm.astype('float32')/32768


def bundled_seed(row,members):
    _,fixture,entry=source_context(row,members)
    return {'dialogue.json':fixture.member(entry['exact_manifest']),
            **{Path(clip['member']).name:fixture.member(clip) for clip in entry['oggs']}}
