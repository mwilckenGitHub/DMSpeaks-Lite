"""Isolated WoW-companion native-source contract prototype; no core app imports.

The documentary fixture/ownership map are explicit trusted inputs for this trial.
This is not an installed portable schema, a signed release, or a TTS client.
"""
from hashlib import sha256
from pathlib import Path, PurePosixPath
from io import BytesIO
import json
import re
import soundfile as sf

FORMAT = 'dmspeaks-companion-native-source-prototype-1'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(raw):
    return sha256(raw).hexdigest()


class Fixture:
    def __init__(self, root, expected_fixture_sha256, authority, expected_authority_sha256):
        self.root = Path(root).resolve()
        raw = (self.root / 'fixture.json').read_bytes()
        if digest(raw) != expected_fixture_sha256:
            raise ValueError('The admitted documentary fixture changed.')
        self.data = json.loads(raw)
        raw_authority = Path(authority).read_bytes()
        if digest(raw_authority) != expected_authority_sha256 or self.data['source_map']['sha256'] != expected_authority_sha256:
            raise ValueError('The independent source-ownership map changed.')
        self.authority = {x['ordinal']: x for x in json.loads(raw_authority)['rows']}
        self.entries = {x['ordinal']: x for x in self.data['entries']}

    def member(self, descriptor):
        name = descriptor['member']
        route = PurePosixPath(name)
        if route.is_absolute() or '..' in route.parts or '\\' in name:
            raise ValueError('A fixture member leaves the admitted package.')
        path = self.root.joinpath(*route.parts)
        cursor = self.root
        for component in route.parts:
            cursor = cursor / component
            if cursor.is_symlink():
                raise ValueError('A fixture member may not traverse a symlink.')
        if path.resolve() != path or not path.is_file():
            raise ValueError('A fixture member is missing or linked outside the package.')
        raw = path.read_bytes()
        if digest(raw) != descriptor['sha256'] or self.data['files'][name]['sha256'] != descriptor['sha256']:
            raise ValueError('A fixture member differs from the admitted bytes.')
        return raw


def admitted_contract(fixture, ordinal):
    e = fixture.entries[ordinal]
    authority = fixture.authority[ordinal]
    if authority['native_owner_conflict'] or authority['status'].startswith('hold_'):
        raise ValueError('The native source owner is not independently admitted.')
    for key in ('source_id', 'npc_id', 'locale', 'native_key', 'native_stage'):
        if e[key] != authority[key]:
            raise ValueError('The fixture and independent native owner disagree: ' + key)
    text = e['original_corpus_text']
    if digest(text.encode()) != e['original_corpus_text_sha256'] or text != authority['original_text']:
        raise ValueError('The original corpus text differs from its admitted source.')
    manifest = json.loads(fixture.member(e['exact_manifest']))
    plan = json.loads(fixture.member(e['exact_plan']))
    clock = json.loads(fixture.member(e['base_alignment']))
    for key in ('source_id', 'npc_id', 'locale'):
        if manifest[key] != e[key] or plan['row'][key] != e[key]:
            raise ValueError('A playback/plan owner differs from the native corpus source.')
    if (manifest['spoken_key'] != e['native_key'] or manifest['original_text'] != text
            or plan['row']['original_text'] != text or manifest['original_spoken'] is not None):
        raise ValueError('The native source must retain its actual key/text without a fabricated stock binding.')
    wave = fixture.member(e['generated_base'])
    samples, rate = sf.read(BytesIO(wave), dtype='int16', always_2d=True)
    pcm = samples.astype('<i2', copy=False).tobytes()
    source = e['source_clock']
    if (samples.shape[1] != 1 or rate != 44100 or len(samples) != source['sample_count']
            or digest(pcm) != source['decoded_pcm_sha256']
            or digest(wave) != manifest['source_wave_sha256']
            or clock['audio_sha256'] != digest(wave) or clock['sample_count'] != len(samples)
            or clock['sample_rate'] != rate or clock['transcript_sha256'] != source['alignment_transcript_sha256']):
        raise ValueError('The native playback base/phoneme clock changed.')
    listed = {PurePosixPath(x['member']).name: x for x in e['oggs']}
    cursor = 0
    for part in manifest['sequence']:
        start, end = part['source_samples']
        if start != cursor or end <= start:
            raise ValueError('The native recipe does not cover the exact source clock.')
        cursor = end
        if part['kind'] == 'fixed':
            clips = [part['clip']]
            if clips[0]['samples'] != end - start:
                raise ValueError('A fixed clip changes source duration.')
        else:
            if part['fallback'] not in part['choices']:
                raise ValueError('A delimiter lacks its own contextual fallback.')
            clips = list(part['choices'].values())
        for clip in clips:
            raw = fixture.member(listed[clip['file']])
            info = sf.info(BytesIO(raw))
            if (digest(raw) != clip['sha256'] or (info.format, info.subtype, info.samplerate, info.channels, info.frames)
                    != ('OGG', 'VORBIS', rate, 1, clip['samples'])):
                raise ValueError('The native recipe contains changed or mistimed Ogg media.')
    if cursor != source['sample_count'] or manifest['source_samples'] != cursor:
        raise ValueError('The native recipe ends on another source clock.')
    reference = e['reference_evidence']['reference']
    reference_raw=fixture.member(reference)
    reference_pcm,reference_rate=sf.read(BytesIO(reference_raw),dtype='int16',always_2d=True)
    if reference_rate!=44100 or reference_pcm.shape[1]!=1 or not 0<len(reference_pcm)<=44100*120:
        raise ValueError('The retained voice reference has an unsupported clock or exceeds120seconds.')
    declared=plan['reference']
    admitted_reference=authority['reference_provenance']
    if (reference['sha256'] != manifest['reference_sha256']
            or declared['sha256'] != reference['sha256']
            or admitted_reference['reference']['sha256'] != reference['sha256']
            or declared['reference_text'] != admitted_reference['transcript']):
        raise ValueError('The original admitted voice reference changed.')
    transcript=declared['reference_text']
    declarations=[]
    if profile:=e['reference_evidence'].get('profile_declaration'):
        fixture.member(profile)
        declarations.append(dict(kind='retained-profile-declaration',sha256=profile['sha256']))
    if acquisition:=e.get('later_original_game_reference',{}).get('declaration'):
        fixture.member(acquisition)
        declarations.append(dict(kind='retained-acquisition-declaration',sha256=acquisition['sha256']))
    prefix=e['source_id'].partition(':')[0]
    if prefix not in {'q', 'g', 'f'}:
        raise ValueError('The native source has an unsupported event kind.')
    route = dict(kind={'q': 'quest', 'g': 'gossip', 'f': 'followup'}[prefix],
                 key=e['native_key'], event=e['native_stage'], npc_id=e['npc_id'],
                 original_text_sha256=e['original_corpus_text_sha256'])
    if prefix == 'f':
        match = re.fullmatch(r'f:([1-9][0-9]*):model-([1-9][0-9]*)', e['source_id'])
        if not match or not acquisition:
            raise ValueError('The native follow-up needs its exact campaign line and model.')
        proof = json.loads(fixture.member(acquisition))['followup']
        broadcast_id, model_data_id = map(int, match.groups())
        if (e['native_stage'] != 'followup' or e['native_key'] != f'{broadcast_id}-model-{model_data_id}'
                or (proof['broadcast_id'], proof['model_data_id'], proof['speaker'], proof['quest_id'])
                   != (broadcast_id, model_data_id, e['npc_id'], plan['row']['quest_id'])
                or (broadcast_id, proof['quest_id']) !=
                   (authority['owner_evidence']['followup_line_id'], authority['owner_evidence']['corpus_quest_id'])
                or proof['original_text_sha256'] != route['original_text_sha256']
                or proof['phase'] not in {'start', 'end'}
                or type(proof['model_file_id']) is not int or proof['model_file_id'] <= 0):
            raise ValueError('The native follow-up has a different trigger, speaker or model.')
        route.update(event=None, broadcast_id=broadcast_id, model_file_id=proof['model_file_id'])
    contract = dict(format=FORMAT, source_kind='corpus-native', ordinal=ordinal,
                    source_id=e['source_id'], npc_id=e['npc_id'], locale=e['locale'],
                    route=route,
                    original_text=text, stock_binding=None,
                    source_plan_sha256=e['exact_plan']['sha256'],
                    base=dict(wav_sha256=digest(wave), pcm_sha256=digest(pcm), sample_rate=rate,
                              samples=len(samples), phoneme_clock_sha256=e['base_alignment']['sha256']),
                    recipe_sha256=e['exact_manifest']['sha256'],
                    voice_reference=dict(sha256=reference['sha256'], transcript=transcript,
                                         transcript_sha256=digest(transcript.encode()),
                                         sample_rate=reference_rate,samples=len(reference_pcm),
                                         pcm_sha256=digest(reference_pcm.astype('<i2',copy=False).tobytes()),
                                         retained_declarations=declarations,
                                         declaration_policy='pinned-retained-source-reference-awaiting-provider-compilation-1',
                                         ordered_references=[dict(id=e['source_id']+':retained-reference',
                                                                  audio_sha256=reference['sha256'],
                                                                  transcript_sha256=digest(transcript.encode()))],
                                         declaration_scope='Exact source-plan and pinned authority declaration; not independent acquisition or provider-policy approval.',
                                         classification=e['voice_reference_classification'],
                                         review_pending=e['reference_review_pending']),
                    owner_evidence_sha256=e['ownership_evidence']['sha256'],
                    eligibility=dict(authority_sha256=e['eligibility_authority']['sha256'],
                                     applicability_resolved=False),
                    declared_occurrences=e['available_occurrences'])
    fixture.member(e['ownership_evidence']); fixture.member(e['eligibility_authority'])
    return contract


def validate_contract(fixture, proposed):
    """Reconstruct expected contract from independent admitted inputs, never itself."""
    expected = admitted_contract(fixture, proposed['ordinal'])
    if canonical(proposed) != canonical(expected):
        raise ValueError('The native source contract differs from independently admitted audio/text/owner evidence.')
    return expected


def name_request_plan(fixture, contract, name):
    validate_contract(fixture, contract)
    if not re.fullmatch(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", name, re.UNICODE):
        raise ValueError('Use a single first name without source markup.')
    entry = fixture.entries[contract['ordinal']]
    plan = json.loads(fixture.member(entry['exact_plan']))
    jobs = []
    for job in plan['jobs']:
        if job['kind'] != 'name':
            continue
        lo, hi = job['context']; a, b = job['slot']['start'], job['slot']['end']
        if not lo <= a < b <= hi <= len(plan['stock_text']):
            raise ValueError('The name occurrence is outside its actual source context.')
        text = (plan['stock_text'][lo:a] + name + plan['stock_text'][b:hi]).strip()
        if re.search(r'\$[nNcCrRgGbB]', text):
            raise ValueError('Unresolved corpus markup would be sent as speech.')
        jobs.append(dict(occurrence=job['occurrence'], spoken_name=name, text=text,
                         source_context=[lo, hi], source_name_span=[a, b],
                         source_contract_sha256=digest(canonical(contract)),
                         state='not-submitted', generated_audio=False))
    return dict(source_id=contract['source_id'], name=name, requests=jobs,
                fallback='existing-contextual-Adventurer',
                source_contract_verified=True, player_applicability_resolved=False,
                submission_blockers=['player-applicability-not-resolved', 'provider-reference-policy-not-compiled']
                                    + (['voice-reference-review-pending']
                                       if contract['voice_reference']['review_pending'] else []),
                provider_requests=0, fresh_name_clip_ready=False)
