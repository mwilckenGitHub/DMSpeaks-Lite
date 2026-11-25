from performance_plan import parse_record
"""Corpus → full take → native candidate edits → independently selectable library."""
import array,copy,hashlib,json,math,uuid,urllib.parse,re,os
from pathlib import Path
from audio import decode,encode_preview_pcm
from delivery_timing import measure_delivery
from alignment_backend import source_alignment_fingerprint
from model import parse_segments,segment_audio_identity,segment_audio_matches,clean_spoken
from speech_edit import codec_window
from speech_edit_authoring import build_request,prepare_lossless_source
from speech_edit_client import SpeechEditClient,PendingSpeechEdit
from library_export import build_library
from fish_community_tts import has_attention_header_clock


def client_for(app,provider):
    profile=app.connections.profile(provider)
    if profile.get('engine')=='fish_omni':
        from fish_community_edit import CommunitySpeechEditClient
        config=Path(os.environ.get('DMSPEAKS_FISH_EDIT_CONFIG',str(Path(__file__).resolve().parent.parent/'local-work/community-edit-runtime.json')))
        if not config.is_file():raise ValueError('Install the community Fish native-edit bridge configuration. No legacy worker or ordinary TTS fallback was selected.')
        return CommunitySpeechEditClient(app.connections.endpoint(profile),config)
    if profile.get('engine')!='fish_resident' or profile.get('transport','local')!='local':raise ValueError('This prototype currently has a local Fish infilling adapter. Other connections need an adapter advertising the contextual-edit protocol; ordinary TTS is not used as a fallback.')
    endpoint=app.connections.endpoint(profile)
    if urllib.parse.urlsplit(endpoint).hostname not in ('127.0.0.1','localhost','::1'):raise ValueError('The local Fish adapter requires a loopback endpoint because its source assets are local files.')
    return SpeechEditClient(endpoint,app.providers._request,poll_seconds=.5)

def normalize_adjacent_windows(draft,segments,capabilities):
    """Partition an estimated inter-word gap on an exact shared codec boundary.

    No waveform is appended to generated inserts. A boundary must lie entirely
    between the supplied word intervals; otherwise independent clips are refused.
    Estimated word times still require acoustic/listening review.
    """
    source=draft['request']['source'];sr=source['sample_rate'];rate=capabilities.get('codec_sample_rate');hop=capabilities.get('codec_frame_samples')
    if type(rate) is not int or type(hop) is not int or rate<=0 or hop<=0:raise ValueError('The adapter must advertise codec frame coordinates.')
    slots={s['slot_id']:s for s in draft['request']['slots']};bindings=draft['bindings'];period=rate//math.gcd(hop*sr,rate)
    for left,right in zip(bindings,bindings[1:]):
        if right['slot_order']!=left['slot_order']+1:continue
        end=left['source_end_sample'];begin=right['source_start_sample']
        if end>begin:raise ValueError('Adjacent word intervals overlap. Review their alignment before independent editing.')
        lo=(end*rate+sr*hop-1)//(sr*hop);hi=(begin*rate)//(sr*hop)
        first=((lo+period-1)//period)*period;last=(hi//period)*period
        if first>last:raise ValueError('Adjacent replacements have no shared codec boundary between their word intervals. Review or widen the inter-word gap before independent editing.')
        frame=max(first,min(last,round(((end+begin)*rate)/(2*sr*hop*period))*period));boundary=frame*hop*sr//rate
        for binding,key in ((left,'source_end_sample'),(right,'source_start_sample')):
            binding.setdefault('word_start_sample',binding['source_start_sample']);binding.setdefault('word_end_sample',binding['source_end_sample'])
            binding[key]=boundary;binding['boundary_method']='shared codec boundary within estimated inter-word gap'
            if binding['slot_id'] in slots:slots[binding['slot_id']][key]=boundary
    return draft


def assert_source_performance(app,record,data,asset):
    trace=asset.get('generation',{}).get('request')
    if isinstance(trace,dict) and asset.get('generation_key'):
        expected={**data,'variant':trace.get('variant') or 'default','seed':trace.get('seed') if trace.get('seed') is not None else 42,'rate':trace.get('rate') or 1}
        if app.generation_key(record,expected)!=asset['generation_key']:raise ValueError('The full take does not match the selected speaker, reference or connection. Explicitly render a matching baseline before editing its words.')
    elif data.get('voice') or data.get('reference_id'):
        raise ValueError('This older full take has no traceable voice settings. Choose a traceable baseline before applying a voice/reference override.')


def context_for(app,record,data,asset):
    hint_policy=data.get('duration_hint_policy','none');alignment_digest=source_alignment_fingerprint(asset)
    parsed=parse_record(record,asset.get('player',{}),{'stage_directions':asset.get('generation',{}).get('stage_direction_format','auto')})['segments'];adjacent=any(a['type']!='fixed' and b['type']!='fixed' for a,b in zip(parsed,parsed[1:]))
    # New plans must not reuse legacy Ogg pieces with a baked-in edge envelope.
    # Existing plans and recordings retain their original identities.
    suffix='-safe-gaps1-lossless1'+('-adjacent1' if adjacent else '')+('-lexical1' if any(s.get('literal_suffix') for s in parsed) else '')
    if has_attention_header_clock(asset):
        suffix += '-attention-frame1'
    source_digest=asset.get('sha256') or hashlib.sha256(Path(asset['path']).read_bytes()).hexdigest()
    context={**copy.deepcopy(data),'variant':'infill-'+source_digest[:24]+'-'+alignment_digest[:12]+suffix+('-source-hint' if hint_policy=='source_span' else ''),'rate':1}
    return context,app.generation_key(record,context),alignment_digest


def plan(app,data):
    record=app.record(data);asset=app.store.asset(data['source_asset_id'])
    if data.get('mode')=='names':
        from batch_variables import excluded_names
        domains=data.get('domains',{})
        if not domains or set(domains)-excluded_names(record):
            raise ValueError('Name updates may request only independent person-name occurrences.')
    from content_review import assert_audio_usable
    assert_audio_usable(asset,app.store)
    assert_source_performance(app,record,data,asset)
    if type(data.get('seed',42)) is not int:raise ValueError('Choose an integer seed.')
    if float(data.get('rate',1))!=1:raise ValueError('Native infilling retains the source pace. Use playback/layer speed, or regenerate the baseline at a new pace.')
    context,key,alignment_digest=context_for(app,record,data,asset)
    prepared=prepare_lossless_source(app,asset)
    draft=build_request(record,prepared,data.get('domains',{}),data.get('duration_hint_policy','none'),
                        target=data.get('target'),requested_orders=data.get('requested_orders'))
    ident=uuid.uuid4().hex
    try:capabilities=client_for(app,data['provider']).capabilities();available=True;reason='Native infilling endpoint is available.'
    except (ValueError,OSError) as error:capabilities={};available=False;reason=str(error)
    codec_bounds, codec_windows = prepare_codec_windows(record, asset, draft, capabilities) if available else ([], {})
    stored={'kind':'native_speech_edit','boundary_policy':'codec_in_estimated_gaps_v1','record_data':context,'source_asset_id':asset['id'],'source_alignment_sha256':alignment_digest,'source_original_sha256':hashlib.sha256(Path(asset['path']).read_bytes()).hexdigest(),'draft':draft,'generation_key':key,'source_revision':record['revision'],'source_metadata_revision':record.get('metadata_revision'),'codec_windows':codec_windows}
    stored['codec_bounds'] = codec_bounds
    app.plans[ident]=stored
    saved_path=app.folder/'edit-plans'/(ident+'.json');saved_path.parent.mkdir(parents=True,exist_ok=True);saved_path.write_text(json.dumps(stored,ensure_ascii=False,indent=2))
    return {'plan_id':ident,'request':draft['request'],'bindings':draft['bindings'],'variant':context['variant'],'edit_count':sum(len(s['replacements']) for s in draft['request']['slots']),'capabilities':capabilities,'provider_available':available,'provider_notice':reason,'notice':'The source take supplies voice and performance. Each candidate is an independent edit; baseline values are reused. Full dialogue combinations are not generated. Estimated boundaries and new joins require listening review.'}


def prepare_codec_windows(record, asset, draft, capabilities):
    """Derive the renderer's exact codec windows from the estimated word intervals."""
    segments = parse_record(record, asset.get('player', {}), {
        'stage_directions': asset.get('generation', {}).get('stage_direction_format', 'auto'),
    })['segments']
    normalize_adjacent_windows(draft, segments, capabilities)
    return regions({'draft': draft}, segments, capabilities)


def regions(plan,segments,capabilities):
    request=plan['draft']['request'];rate=request['source']['sample_rate'];count=request['source']['sample_count']
    codec_rate=capabilities.get('codec_sample_rate');hop=capabilities.get('codec_frame_samples')
    if type(codec_rate) is not int or type(hop) is not int or codec_rate<=0 or hop<=0:raise ValueError('The native adapter must advertise its codec frame coordinates before library assembly.')
    frames=math.ceil(count*codec_rate/rate/hop);windows={};bounds=[None]*(len(segments)+1);bounds[0]=0;bounds[-1]=count
    def boundary(index,value):
        if bounds[index] is not None and bounds[index]!=value:raise ValueError('Adjacent replacement windows share a codec frame. Review their boundaries before generating independent clips.')
        bounds[index]=value
    for binding in plan['draft']['bindings']:
        order=binding['slot_order'];window=codec_window(binding['source_start_sample'],binding['source_end_sample'],count,rate,codec_rate,hop,frames);windows[binding['slot_id']]=window
        if order>1 and segments[order-2]['type']=='fixed':
            previous=binding.get('preceding_word_end_sample')
            if previous is None:raise ValueError('This older edit plan has no neighboring-word boundary checks. Scan the retained take again.')
            if window['quantized_start_sample']<previous:raise ValueError('The codec edit window would cut the preceding fixed word interval. Review the boundary or its alignment before independent editing.')
        if order<len(segments) and segments[order]['type']=='fixed':
            following=binding.get('following_word_start_sample')
            if following is None:raise ValueError('This older edit plan has no neighboring-word boundary checks. Scan the retained take again.')
            if window['quantized_end_sample']>following:raise ValueError('The codec edit window would cut the following fixed word interval. Review the boundary or its alignment before independent editing.')
        if order>1:boundary(order-1,window['quantized_start_sample'])
        if order<len(segments):boundary(order,window['quantized_end_sample'])
    if any(v is None for v in bounds) or any(a>b for a,b in zip(bounds,bounds[1:])):raise ValueError('Codec edit windows conflict; review the source boundaries.')
    return bounds,windows

def _existing_piece(app,record,context,key,segment,value):
    """Validate matching cached delivery before spending another model request."""
    from clip_ownership import belongs_to_record
    for asset in app.store.pieces_for_line(record['id']):
        if belongs_to_record(asset,record) and asset.get('dataset')==record['dataset'] and asset.get('voice')==record['metadata'].get('npc_voice_profile') and asset.get('slot_order')==segment['order'] and segment_audio_matches(asset,segment) and asset.get('variant')==context['variant'] and asset.get('generation_key')==key and asset.get('value','')==value and asset.get('source_revision')==record['revision'] and asset.get('source_metadata_revision')==record.get('metadata_revision') and Path(asset['path']).is_file():
            from content_review import is_rejected
            if is_rejected(asset):raise ValueError('A matching native piece was rejected. Review it or change the source/seed before generating another take.')
            if asset.get('sha256') and hashlib.sha256(Path(asset['path']).read_bytes()).hexdigest()!=asset['sha256']:raise ValueError('A previously generated native piece changed. Review that asset before reuse.')
            return asset
    return None

def _piece(app,record,context,key,source_asset,segment,pcm,value,provenance,expected_text=None,context_window=None):
    if not pcm:return None
    existing=_existing_piece(app,record,context,key,segment,value)
    if existing:return existing
    # Keep source onsets/decays intact. The editor owns optional envelopes;
    # delivery export owns lossy encoding. No fade belongs in an imported master.
    ident=uuid.uuid4().hex;target=app.audio.root/(ident+'.flac');encode_preview_pcm(pcm,target,sr=44100);measured=measure_delivery(target)
    # Keep the optional voice identity identical to the corpus. Candidate lookup
    # distinguishes a missing profile (None) from an explicitly empty profile.
    metadata={'id':ident,'kind':'piece','path':str(target),'dataset':record['dataset'],'line_id':record['id'],'npc_id':record['metadata'].get('npc_id'),'voice':record['metadata'].get('npc_voice_profile'), 'variant':context['variant'],'reference_id':context.get('reference_id','') or '','generation_key':key,'source_revision':record['revision'],'source_metadata_revision':record.get('metadata_revision'),'slot_order':segment['order'],'slot_type':segment['type'],'value':value,'sample_rate':44100,'channels':1,'sample_count':measured['audio_sample_count'],'seconds':measured['audio_seconds'],'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'original_sample_count':len(pcm),'original_sample_rate':44100,'original_seconds':len(pcm)/44100,'source_take_asset_id':source_asset['id'],'source_alignment_sha256':source_alignment_fingerprint(source_asset),'alignment':source_asset.get('alignment'),'words':[],'timing_source':'codec_window_only','listening_review':'pending','isolation_review_required':True,'insert_expected_text':segment.get('spoken_text','') if segment['type']=='fixed' else value,'native_edit_provenance':provenance,'boundary_method':'codec-window partition; no implicit fade or added silence'}
    metadata['record_id'] = source_asset['record_id']
    metadata['master_processing']={'format':'flac','pcm_bits':24,'implicit_fades':False,'time_stretch':False,'added_silence_samples':0}
    metadata.update(segment_audio_identity(segment));metadata['insert_expected_text']=segment.get('spoken_text','') if expected_text is None else clean_spoken(expected_text)
    if context_window is not None and segment['type'] != 'fixed' and source_asset.get('sha256'):
        metadata['native_context_window'] = {
            'format': 'native-context-window-1', 'source_sha256': source_asset['sha256'],
            'placeholder': segment['placeholder'], 'start_sample': context_window[0], 'end_sample': context_window[1],
        }
    return app.store.add_asset(metadata)

def load_plan(app, ident):
    if not isinstance(ident,str) or not re.fullmatch(r'[0-9a-f]{32}',ident):raise ValueError('Choose a valid saved native-edit plan.')
    saved=app.plans.get(ident)
    path=app.folder/'edit-plans'/(ident+'.json')
    if saved is None and path.is_file():saved=json.loads(path.read_text());app.plans[ident]=saved
    if not saved or saved.get('kind')!='native_speech_edit':raise ValueError('This contextual edit plan expired. Build it again.')
    if saved.get('boundary_policy')!='codec_in_estimated_gaps_v1':raise ValueError('This older edit plan predates neighboring-word boundary checks. Retained diagnostics remain available; scan the original take again before rendering.')
    return saved, path


def context_audition_source(app, data):
    """Bind an approved clip and its exact codec window to the original take."""
    if 'plan_id' not in data:
        return saved_context_audition_source(app, data)
    saved, _ = load_plan(app, data.get('plan_id'))
    context = saved['record_data']
    record = app.record(context)
    source = app.store.asset(saved['source_asset_id'])
    piece = app.store.asset(data['asset_id'])
    slot_id = 'segment-' + str(piece.get('slot_order'))
    binding = next((item for item in saved['draft']['bindings'] if item['slot_id'] == slot_id), None)
    requested = any(item['slot_id'] == slot_id for item in saved['draft']['request']['slots'])
    if (not binding or not requested or piece.get('kind') != 'piece' or
            piece.get('slot_type') != binding['kind'] or piece.get('source_take_asset_id') != source['id'] or
            piece.get('generation_key') != saved['generation_key'] or
            piece.get('value') not in binding['replacement_values'].values()):
        raise ValueError('Choose a replacement clip rendered for this plan and occurrence.')
    alignment = source_alignment_fingerprint(source)
    if (alignment != saved['source_alignment_sha256'] or alignment != piece.get('source_alignment_sha256') or
            source.get('sha256') != saved['source_original_sha256']):
        raise ValueError('The source take or its alignment changed. Build a fresh replacement plan.')
    if any(asset.get('sample_rate') != 44100 or asset.get('channels') != 1 for asset in (source, piece)):
        raise ValueError('Context preview needs 44.1 kHz mono source and replacement audio.')
    provenance = piece.get('native_edit_provenance') or {}
    window = saved['codec_windows'].get(slot_id)
    if provenance.get('method') == 'preserved_baseline':
        start, end = provenance.get('source_start_sample'), provenance.get('source_end_sample')
    elif window and all((provenance.get('source_window') or {}).get(key) == window[key]
                        for key in ('start_frame', 'end_frame', 'codec_rate', 'hop_samples')):
        start, end = window['quantized_start_sample'], window['quantized_end_sample']
    else:
        raise ValueError('This clip has no matching codec window. Build a fresh replacement plan.')
    if not (type(start) is int and type(end) is int and 0 <= start <= end <= source['sample_count']):
        raise ValueError('The saved replacement window is outside the source take.')
    return record, source, piece, binding, start, end


def saved_context_audition_source(app, data):
    record = app.record(data)
    piece = app.store.asset(data['asset_id'])
    receipt = piece.get('native_context_window')
    if piece.get('kind') != 'piece' or not isinstance(receipt, dict) or receipt.get('format') != 'native-context-window-1':
        raise ValueError('This clip has no saved context window. Open its original replacement plan.')
    source = app.store.asset(piece.get('source_take_asset_id', ''))
    record_id = f"{record['dataset']}:{record['index']}"
    if (source.get('kind') != 'take' or (source.get('generation') or {}).get('mode') != 'context' or
            source.get('record_id') != record_id or piece.get('record_id') != record_id or
            source.get('sha256') != receipt.get('source_sha256') or
            source_alignment_fingerprint(source) != piece.get('source_alignment_sha256')):
        raise ValueError('The saved clip does not match this dialogue and original take.')
    for asset in (source, piece):
        if asset.get('sample_rate') != 44100 or asset.get('channels') != 1:
            raise ValueError('Context preview needs 44.1 kHz mono source and replacement audio.')
    segments = parse_record(record, source.get('player', {}), {
        'stage_directions': source['generation'].get('stage_direction_format', 'auto'),
    })['segments']
    segment = next((item for item in segments if item['order'] == piece.get('slot_order')), None)
    if (not segment or segment['type'] == 'fixed' or segment.get('speech_excluded') or
            segment['type'] != piece.get('slot_type') or segment['placeholder'] != receipt.get('placeholder') or
            not segment_audio_matches(piece, segment)):
        raise ValueError('The saved replacement occurrence changed.')
    start, end = receipt.get('start_sample'), receipt.get('end_sample')
    if not (type(start) is int and type(end) is int and 0 <= start <= end <= source['sample_count']):
        raise ValueError('The saved replacement window is outside the source take.')
    provenance = piece.get('native_edit_provenance') or {}
    if provenance.get('method') == 'preserved_baseline':
        expected = (provenance.get('source_start_sample'), provenance.get('source_end_sample'))
    else:
        window = provenance.get('source_window') or {}
        first, last, rate, hop = (window.get(key) for key in ('start_frame', 'end_frame', 'codec_rate', 'hop_samples'))
        if not (all(type(value) is int and 0 <= value <= 2**32 for value in (first, last, rate, hop))
                and first <= last and rate and hop):
            raise ValueError('The saved clip has no verifiable codec coordinates.')
        expected = (round(first * hop / (rate / 44100)),
                    min(source['sample_count'], round(last * hop / (rate / 44100))))
    if (start, end) != expected:
        raise ValueError('The saved context window differs from the recorded replacement window.')
    binding = {'slot_order': segment['order'], 'kind': segment['type'], 'original_delimiter': segment['placeholder']}
    return record, source, piece, binding, start, end


def preview_in_context(app, data):
    """Audition an approved insert at the exact codec window of its original take."""
    from contextual_return import checked_pcm
    from tempfile import TemporaryDirectory

    record, source, piece, _, start, end = context_audition_source(app, data)
    base_pcm = checked_pcm(app, record, source)
    insert_pcm = checked_pcm(app, record, piece)
    base_pcm[start:end] = insert_pcm
    with TemporaryDirectory(prefix='dmspeaks-native-audition-') as folder:
        path = Path(folder) / 'preview.flac'
        encode_preview_pcm(base_pcm, path)
        return path.read_bytes()


def open_context_tracks(app, data, status):
    """Create a separate editable inspection arrangement from the audited window."""
    from contextual_return import checked_pcm

    if data.get('suggested_join') is True:
        return inspect_measured_join(app, data, status)

    record, source, piece, binding, start, end = context_audition_source(app, data)
    checked_pcm(app, record, source)
    checked_pcm(app, record, piece)
    return _build_context_tracks(app, record, source, piece, binding, start, end, status)


def inspect_measured_join(app, data, status):
    """Open an audition using word evidence bound to the retained codec decode."""
    from content_review import assert_audio_usable, media_digest
    from layered_audio import LayerSourceCache
    from native_join_evidence import context_path, read
    from native_join_proposal import propose, provider_evidence
    from alignment_backend import bind_word_times
    from model import char_word_mapping, delimiter_source_end
    from speech_edit_authoring import delimiter_text_spans
    from spoken_spans import normalized_spoken_spans

    record, source, piece, binding, _, _ = context_audition_source(app, data)
    for asset in (source, piece):
        assert_audio_usable(asset, app.store, audition_only=True)
        if media_digest(asset) != asset['sha256']:
            raise ValueError('A recording changed after the timing evidence was saved.')
    native = piece.get('native_edit_provenance') or {}
    context = native.get('full_context') or {}
    path = context_path(app.store, piece)
    if path is None:
        raise ValueError('The original codec decode for this replacement is unavailable.')
    if media_digest({'path': path}) != context['sha256']:
        raise ValueError('The original codec decode changed.')

    generated = (native['generated_start_sample_in_context'], native['generated_end_sample_in_context'])
    cache = LayerSourceCache()
    original_pcm, source_channels = cache.read(source['path'])
    insert_pcm, insert_channels = cache.read(piece['path'])
    if source_channels != 1 or insert_channels != 1:
        raise ValueError('Join review needs mono source and replacement recordings.')
    context_pcm = decode(path)
    if insert_pcm != context_pcm[generated[0]:generated[1]]:
        raise ValueError('The saved replacement does not match the generated samples.')
    match_padding = data.get('match_padding', False)
    if type(match_padding) is not bool:
        raise ValueError('Choose whether to match quiet padding.')
    min_dbfs = data.get('min_dbfs', -45)
    if type(min_dbfs) not in (int, float) or not -75 <= min_dbfs <= -30:
        raise ValueError('Choose a quiet-join threshold between −75 and −30 dBFS.')
    from replacement_snap import requested_mode
    snap_mode = requested_mode(data)
    segments = parse_record(record, source.get('player', {}), {
        'stage_directions': source['generation'].get('stage_direction_format', 'auto'),
    })['segments']
    transcript, spans = normalized_spoken_spans(''.join(s['resolved_text'] for s in segments))
    if transcript != source['generation']['spoken_text']:
        raise ValueError('The source transcript changed after generation.')
    occurrence = next(((segment, lo, hi) for segment, _, _, lo, hi
                       in delimiter_text_spans(segments, transcript, spans)
                       if segment['order'] == binding['slot_order']), None)
    if occurrence is None:
        raise ValueError('The selected replacement occurrence changed.')
    segment, char_start, char_end = occurrence
    binding = {**binding, 'source_char_start': char_start, 'source_char_end': char_end,
               'source_start': segment['source_start'], 'lexical_source_end': delimiter_source_end(segment)}
    def check(evidence, *, isolated=None):
        return propose(source, original_pcm, evidence['source_evidence'], context, context_pcm,
                       evidence['context_evidence'], binding, piece['insert_expected_text'], generated,
                       recognized_as=evidence.get('recognized_as'), min_dbfs=min_dbfs,
                       insert_evidence=(isolated, piece) if isolated is not None else None)

    phone_error = None
    proposal = None
    if (hasattr(app, 'align_phonemes')
            and not ('source_evidence' in data and 'context_evidence' in data and not data.get('suggested_join'))):
        from phoneme_snap import PhonemeJoinRejected, validate_pcm, propose as propose_phoneme_join

        validate_pcm(original_pcm, source)
        validate_pcm(context_pcm, context)
        replacement = piece['insert_expected_text']
        target = transcript[:char_start] + replacement + transcript[char_end:]
        try:
            source_phones = app.align_phonemes(record, source, transcript)
            return_phones = app.align_phonemes(record, context, target, path=path)
            proposal = propose_phoneme_join(source, context, source_phones, return_phones,
                                            binding, replacement, generated,
                                            source_pcm=original_pcm, context_pcm=context_pcm)
        except PhonemeJoinRejected:
            # Missing timing may fall back; contradictory measured evidence
            # requires review instead of accepting a weaker join silently.
            raise
        except ValueError as error:
            phone_error = str(error)

    if proposal is not None:
        evidence = {'source_evidence': {'words': proposal.pop('base_words')}}
    elif 'source_evidence' in data and 'context_evidence' in data and not data.get('suggested_join'):
        evidence = data
        proposal = {**check(evidence), 'timing_basis': 'supplied_word_evidence'}
    else:
        try:
            evidence = provider_evidence(source, context, binding, piece['insert_expected_text'],
                                         generated, native.get('raw_timestamps_header'))
            proposal = {**check(evidence), 'timing_basis': 'provider'}
        except ValueError as provider_error:
            try:
                evidence = read(app.store, piece)
            except ValueError as evidence_error:
                raise ValueError(f'No suggested join from the provider timings. {provider_error} '
                                 f'{evidence_error}') from None
            proposal = {**check(evidence, isolated=evidence.get('insert_evidence')),
                        'timing_basis': evidence.get('timing_basis', 'saved_recognition')}
    phoneme_join = proposal['timing_basis'] == 'phoneme_alignment'
    if phone_error is not None:
        proposal['phoneme_fallback_reason'] = phone_error
    if match_padding and not phoneme_join:
        from native_pause_match import match_proposal
        proposal = match_proposal(original_pcm, insert_pcm, proposal, min_dbfs)
    proposal['assembly_settings'] = {'match_padding': match_padding, 'min_dbfs': min_dbfs}
    if 'snap_mode' in data:
        proposal['assembly_settings']['snap_mode'] = snap_mode
    try:
        base_words = bind_word_times(evidence['source_evidence']['words'], char_word_mapping(transcript, segments),
                                     source['sample_count'] / 44100)
        if phoneme_join:
            base_timing = 'estimated_phoneme_alignment'
        else:
            base_timing = (source.get('timing_source') or 'unavailable'
                           if proposal['timing_basis'] == 'provider' else 'estimated_join_word_evidence')
    except ValueError:
        # A local word match can locate a valid join even when the full
        # recognition differs. Retain previously mapped words elsewhere.
        base_words = source.get('words', [])
        base_timing = source.get('timing_source') or 'unavailable'
    start, end = proposal['source_region']
    preserve_base = phoneme_join or data.get('preserve_base') is True
    return _build_context_tracks(app, record, source, piece, binding, start, end, status,
                                 proposal=proposal, source_cache=cache, base_words=base_words,
                                 base_timing=base_timing, preserve_base=preserve_base,
                                 snap_options=None if preserve_base or phoneme_join else {
                                     key: data[key] for key in ('snap_mode', 'match_padding') if key in data})


def _build_context_tracks(app, record, source, piece, binding, start, end, status, *,
                          proposal=None, source_cache=None, base_words=None, base_timing=None,
                          snap_options=None, preserve_base=False):
    from content_review import assert_audio_usable, media_digest
    from contextual_delivery import NATIVE_DIALOGUE_MIX
    from layered_audio import RATE, LayerSourceCache, render_layers
    from types import SimpleNamespace

    order = binding['slot_order']
    tracks = [{'id': 'base', 'name': 'Base Speech'},
              {'id': 'replacement', 'name': 'Replacement'}]
    layers = []

    def add_layer(ident, track, asset, begin, stop, slot, *, words=None, timing_source=None):
        if stop <= begin:
            return
        layer = {'id': ident, 'track_id': track, 'asset_id': asset['id'], 'kind': 'audio',
                 'source_in_sample': begin, 'source_out_sample': stop, 'slot_order': slot['order'],
                 'label': slot.get('label', ''), 'slot': slot}
        if layers:
            layer['after_layer_id'] = layers[-1]['id']
        if words is not None:
            layer['native_join_timing'] = {'audio_sha256': asset['sha256'], 'words': words,
                                           'timing_source': timing_source}
        layers.append(layer)

    insert_start, insert_end = (0, piece['sample_count']) if proposal is None else proposal['insert_region']
    owner = {'dataset': record['dataset'], 'line_id': record['id'], 'record_id': source['record_id'],
             'source_revision': record['revision'], 'source_metadata_revision': record.get('metadata_revision')}
    status('Building the editable context preview…')
    created_paths = []
    try:
        store = app.store
        clip = piece
        new_clip = None
        proof = {}
        if proposal is not None:
            cache = source_cache if source_cache is not None else LayerSourceCache()
            samples, channels = cache.read(piece['path'])
            if channels != 1 or not 0 <= insert_start < insert_end <= len(samples):
                raise ValueError('The proposed cut is outside the saved replacement.')
            cut = {'return_sha256': piece['sha256'], 'return_samples': [insert_start, insert_end]}
            fingerprint = json.dumps(['native-join-cut-1', piece['id'], cut], sort_keys=True)
            ident = hashlib.sha256(fingerprint.encode()).hexdigest()[:32]
            try:
                clip = app.store.asset(ident)
            except ValueError:
                target = app.audio.root / (ident + '.flac')
                created_paths.append(target)
                encode_preview_pcm(samples[insert_start:insert_end], target)
                clip = {**owner, 'id': ident, 'kind': 'recording-source', 'path': str(target),
                        'sample_rate': RATE, 'channels': 1, 'sample_count': insert_end - insert_start,
                        'seconds': (insert_end - insert_start) / RATE, 'sha256': media_digest({'path': target}),
                        'value': piece['value'], 'slot_order': order,
                        'insert_expected_text': piece.get('insert_expected_text', piece['value']),
                        'isolation_review_required': True, 'timing_source': 'measured join cut',
                        'source_clip_id': piece['id'], 'source_take_asset_id': source['id'],
                        'native_join_slice': cut}
                new_clip = clip
                store = SimpleNamespace(asset=lambda ident: clip if ident == clip['id'] else app.store.asset(ident))
            if clip.get('native_join_slice') != cut or media_digest(clip) != clip['sha256']:
                raise ValueError('The saved cut changed. Review its source before rebuilding.')
            proof = {'join_proposal': proposal, 'contextual_assembly': {'format': NATIVE_DIALOGUE_MIX}}
            if preserve_base:
                proof['contextual_assembly']['preserve_base'] = True
            insert_start, insert_end = 0, clip['sample_count']
        before_words = after_words = None
        if base_words is not None:
            mapped = [word for word in base_words if type(word.get('segment_order')) is int]
            before_words = [word for word in mapped if word['segment_order'] < order]
            after_words = [word for word in mapped if word['segment_order'] > order]
        add_layer('source-before', 'base', source, 0, start,
                  {'order': max(1, order-1), 'type': 'fixed', 'label': 'Original Lead-In'},
                  words=before_words, timing_source=base_timing)
        insert_id = 'approved-replacement' if proposal is None else 'proposed-replacement'
        slot_span = {key: binding[key] for key in ('source_start', 'lexical_source_end') if key in binding}
        replacement_timing = {
            'provider': 'estimated_fish_join_words',
            'phoneme_alignment': 'estimated_phoneme_alignment',
        }.get((proposal or {}).get('timing_basis'), 'estimated_join_word_evidence')
        add_layer(insert_id, 'replacement', clip, insert_start, insert_end,
                  {'order': order, 'type': binding['kind'], 'placeholder': binding['original_delimiter'],
                   'value': piece['value'], 'spoken_text': piece.get('insert_expected_text', piece['value']),
                   'label': piece['value'], **slot_span},
                  words=(proposal or {}).get('replacement_words'),
                  timing_source=replacement_timing)
        if proposal is not None and start > 0 and replacement_timing != 'estimated_phoneme_alignment':
            layers[-1]['fade_in_samples'] = RATE // 500
        if proposal is not None and snap_options is not None:
            from replacement_snap import select_for_delimiter
            select_for_delimiter(layers[-1], record, snap_options)
            if snap_options.get('match_padding'):
                layers[-1].setdefault('replacement_snap', {})['preserve_source_padding'] = True
        add_layer('source-after', 'base', source, end, source['sample_count'],
                  {'order': order+1, 'type': 'fixed', 'label': 'Original Continuation'},
                  words=after_words, timing_source=base_timing)
        pcm, rendered = render_layers(layers, tracks, store,
                                      normalize_master=proposal is not None and not preserve_base,
                                      audition_only=proposal is not None, source_cache=source_cache)
        path = app.audio.root / (uuid.uuid4().hex + '.flac')
        created_paths.append(path)
        encode_preview_pcm(pcm, path)
        preview = {
            **rendered, **owner, **proof, 'kind': 'inspection-preview', 'audition_only': True,
            'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'sample_rate': RATE,
            'timing_source': rendered.get('timing_source', 'measured native join audition' if proposal is not None
                                          else 'native codec-window audition'),
            'native_context': {'slot_order': order, 'placeholder': binding['original_delimiter'],
                               'value': piece['value']},
            'player': source.get('player', {}), 'missing_slots': [],
        }
        with app.store.lock:
            current = app.record({'dataset': record['dataset'], 'index': record['index']})
            if (current['revision'] != record['revision'] or
                    current.get('metadata_revision') != record.get('metadata_revision')):
                raise ValueError('The dialogue changed while preparing the context preview.')
            if proposal is not None:
                for asset in (source, piece):
                    latest = app.store.asset(asset['id'])
                    assert_audio_usable(latest, app.store, audition_only=True)
                    if media_digest(latest) != asset['sha256']:
                        raise ValueError('The source recording changed while preparing the cut.')
            app.store.add_assets([new_clip, preview] if new_clip is not None else [preview])
        return preview
    except Exception:
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise


def run(app,data,status,*,progress=None):
    if 'package' in data and type(data['package']) is not bool:raise ValueError('Package must be true or false.')
    saved, path = load_plan(app, data.get('plan_id'))
    context=saved['record_data'];record=app.record(context);source_asset=app.store.asset(saved['source_asset_id'])
    target=context.get('target')
    package=data.get('package',not target)
    if target and package:raise ValueError('Use the library exporter to package a complete dialogue.')
    from content_review import assert_audio_usable
    assert_audio_usable(source_asset,app.store)
    if record['revision']!=saved['source_revision'] or record.get('metadata_revision')!=saved['source_metadata_revision'] or app.generation_key(record,context)!=saved['generation_key']:raise ValueError('Source, speaker or connection changed after planning. Build a fresh plan.')
    if hashlib.sha256(Path(source_asset['path']).read_bytes()).hexdigest()!=saved['source_original_sha256']:raise ValueError('The original source recording changed after planning.')
    if saved.get('source_alignment_sha256')!=source_alignment_fingerprint(source_asset):raise ValueError('The source word alignment changed, or this older plan has no alignment identity. Build a fresh plan before generating clips.')
    if progress:
        progress('checking_codec')
    client=client_for(app,context['provider']);caps=client.capabilities();request=saved['draft']['request'];source=request['source']
    if source['sample_rate']!=44100:raise ValueError('Prepare a 44,100 Hz baseline take before this prototype library workflow.')
    parsed=parse_record(record,source_asset.get('player',{}),{'stage_directions':source_asset.get('generation',{}).get('stage_direction_format','auto')});bounds,windows=regions(saved,parsed['segments'],caps)
    if saved.get('codec_windows') and saved['codec_windows'] != windows:
        raise ValueError('The provider codec changed after planning. Check the provider again before rendering.')
    if progress:
        progress('preparing_baseline')
    source_pcm=decode(source['audio_uri']);created=[];by_order={s['order']:s for s in parsed['segments']};bindings={b['slot_id']:b for b in saved['draft']['bindings']}
    # Name updates add only their requested inserts. Fixed speech and all other
    # delimiter assets already belong to the source/library; do not recreate
    # them or replace their review/processing metadata during a name update.
    for segment,begin,end in zip(parsed['segments'],bounds,bounds[1:]):
        if target and (segment['order']!=target['order'] or segment['value'] not in context['domains'].get(segment['type'],[])):
            continue
        if context.get('mode')=='names':
            # Reuse a requested name already spoken in this source without a
            # new model call. Unrequested original names are not added either.
            if segment['value'] not in context.get('domains',{}).get(segment['type'],[]):continue
        if segment['type']!='fixed' and not segment['value']:continue
        asset=_piece(app,record,context,saved['generation_key'],source_asset,segment,source_pcm[begin:end],segment['value'],{'method':'preserved_baseline','source_start_sample':begin,'source_end_sample':end},context_window=(begin,end))
        if asset:created.append(asset)
    filtered=copy.deepcopy(request);filtered['slots']=[]
    for slot in request['slots']:
        binding=bindings[slot['slot_id']];baseline=binding.get('source_spoken_text',binding['source_value']);seen=set();values=[]
        for candidate in slot['replacements']:
            signature=json.dumps({k:v for k,v in candidate.items() if k!='value_id'},sort_keys=True)
            if candidate['text']==baseline or signature in seen:continue
            seen.add(signature)
            player_value=binding.get('replacement_values',{}).get(candidate['value_id'],candidate['text'])
            existing=_existing_piece(app,record,context,saved['generation_key'],by_order[binding['slot_order']],player_value)
            if existing:
                created.append(existing);continue
            values.append(candidate)
        if values:filtered['slots'].append({**slot,'replacements':values})
    batch={'jobs':[]}
    # The provider batch identity includes the complete request. Once submitted,
    # retain that exact array even if some outputs have since entered the cache.
    # Filtering it again on resume would change the durable request signature.
    if filtered['slots']:
        if 'submitted_request' in saved:filtered=copy.deepcopy(saved['submitted_request'])
        else:
            saved['submitted_request']=copy.deepcopy(filtered)
            temporary=path.with_suffix('.next');temporary.write_text(json.dumps(saved,ensure_ascii=False,indent=2));temporary.replace(path)
    if filtered['slots']:
        def report_provider(ident, state):
            status('Native edit '+state+' · '+ident)
            if progress:
                progress({'queued': 'provider_queued', 'running': 'provider_rendering',
                          'submitted': 'provider_rendering', 'response_saved': 'recovering_audio',
                          'not_submitted': 'contacting_provider'}.get(state, 'waiting_for_provider'))
        if progress:
            progress('contacting_provider')
        try:batch=client.batch(filtered,app.folder/'edit-batches'/(data['plan_id']+'.json'),seed=int(context.get('seed',42)),timeout_per_job=1800,on_status=report_provider)
        except PendingSpeechEdit as pending:return {'pending':True,'request_id':pending.request_id,'plan_id':data['plan_id'],'notice':str(pending)}
    current=app.record(context)
    if current['revision']!=record['revision'] or current.get('metadata_revision')!=record.get('metadata_revision') or app.generation_key(current,context)!=saved['generation_key']:raise ValueError('The source or speech settings changed while editing. Raw generated takes were retained; build a fresh plan before packaging them.')
    if source_alignment_fingerprint(app.store.asset(source_asset['id']))!=saved.get('source_alignment_sha256'):
        raise ValueError('The source word alignment changed while rendering. Raw outputs were retained; rebuild the plan before importing clips.')
    if progress:
        progress('checking_returned_audio')
    failed=[]
    submitted_slots={slot['slot_id'] for slot in filtered['slots']}
    for item in batch['jobs']:
        if item.get('slot_id') not in submitted_slots:
            raise ValueError('The provider returned an occurrence that was not requested.')
        if item['status']!='complete':failed.append({'slot_id':item['slot_id'],'value_id':item['value_id'],'error':item.get('error','not complete')});continue
        result=item['result'];binding=bindings[item['slot_id']];window=windows[item['slot_id']]
        if result.get('context_encoding','global_codec')!=request['policy'].get('context_encoding','global_codec'):raise ValueError('Returned context encoding differs from the planned delivery partition. Experimental crop results cannot use a global-frame assembly.')
        actual=result.get('source_window',{})
        if any(actual.get(key)!=window[key] for key in ('start_frame','end_frame','codec_rate','hop_samples')):raise ValueError('Returned infilling window differs from the baseline clip partition.')
        if result.get('source_sha256')!=source['sha256']:raise ValueError('The native result belongs to a different source recording.')
        candidate=next(c for s in filtered['slots'] if s['slot_id']==item['slot_id'] for c in s['replacements'] if c['value_id']==item['value_id'])
        chunk=decode(result['output']);order=binding['slot_order'];begin,end=bounds[order-1:order+1]
        # Context is conditioning, never additional output audio. In particular,
        # do not append original heads/tails to a first/last replacement slot.
        pcm=chunk
        player_value=binding.get('replacement_values',{}).get(candidate['value_id'],candidate['text'])
        asset=_piece(app,record,context,saved['generation_key'],source_asset,by_order[order],pcm,player_value,result,
            expected_text=candidate['text'],context_window=(window['quantized_start_sample'],window['quantized_end_sample']))
        if asset:created.append(asset)
    from content_review import is_delivery_eligible
    created=list({asset['id']:asset for asset in created}.values())
    review_required=[a['id'] for a in created if not is_delivery_eligible(a)]
    status('Clips rendered. Isolation review required before assembly or export.' if review_required else 'Packaging reviewed fixed speech and replacement Oggs…' if package else 'Reviewed clips ready; assembly and export are separate.')
    library=None if review_required or failed or not package else build_library(app,{'dataset':record['dataset'],'index':record['index'],'scope':'current','mode':'both','variants':[context['variant']],'silence_ms':int(context.get('silence_ms',200))},status)
    from native_join_evidence import available
    pieces = [{'id': asset['id'], 'order': asset['slot_order'], 'type': asset['slot_type'],
               'value': asset['value'], 'seconds': asset['seconds'],
               'suggested_join_available': available(app.store, asset)} for asset in created]
    return {'pending':False,'plan_id':data['plan_id'],'review_required':review_required,'pieces':pieces,'failed':failed,'library':library,'variant':context['variant'],'generation_key':saved['generation_key'],'reference_id':context.get('reference_id','') or '','source_bounds':bounds,'notice':'Individual clips remain available for diagnostic listening. Unreviewed inserts are excluded from assembly and export. After isolation review, build the clip library; cadence and native WoW playback still need verification.'}
