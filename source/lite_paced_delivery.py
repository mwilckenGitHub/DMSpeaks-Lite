"""Portable typed delivery admission. No raw audio, phoneme or developer-path reads.

Producer admission proves whole-unit transforms. This consumer requires an
externally supplied bundle/index identity and checks the exact sealed projection
and actual delivered Ogg clocks. It never relaxes the native route validator.
"""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from zipfile import BadZipFile

import soundfile as sf
from lite_clip_bounds import saved_cuts

from lite_portable_io import (cache_bytes, cache_file, digest, file_hash, filesystem_path, path_key, read, save,
                              validate_occurrences)
from lite_source_identity import dialogue_source_tuple, quest_wording_key
from lite_expanded_context import BUNDLE as EXPANDED_BUNDLE, FORMAT as EXPANDED_FORMAT

FORMAT='dmspeaks-paced-companion-clock-1'
BUNDLE='dmspeaks-paced-companion-deliveries-1'
PROFILE=dict(numerator=16,denominator=15)
KINDS={'fixed','name','race','class','gender'}
SETTING='spoken_paced_deliveries'
ARCHIVE_BYTES_LIMIT = 2 * 1024 ** 3
ARCHIVE_MEMBER_LIMIT = 100_000
DIALOGUE_LIMIT = 10_000
DELIVERY_FOLDERS = {"q": "quests", "g": "gossip", "f": "followup"}
CAPTION_EVENTS = {"accept": 1, "progress": 2, "complete": 3, "gossip": 5, "followup": 6}


def require(value,message):
    if not value:raise ValueError(message)


def checksum(value):
    require(isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value),'A trusted delivery identity is required.')
    return value


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()


def verify_name_receipts(sequence, receipts):
    require(isinstance(receipts, dict), "The paced Name receipts are malformed.")
    parts = [part for part in sequence if part["kind"] == "name"]
    hashes = {"request_sha256", "donor_sha256", "native_ogg_sha256", "paced_ogg_sha256"}
    for value, items in receipts.items():
        require(isinstance(items, list) and parts and len(items) == len(parts),
                "A paced Name receipt is missing an occurrence.")
        for part, item in zip(parts, items, strict=True):
            choice = part["choices"].get(value)
            require(choice and value != part["fallback"] and isinstance(item, dict)
                    and item.keys() == hashes | {"occurrence"}
                    and item["occurrence"] == part["occurrence"]
                    and item["paced_ogg_sha256"] == choice["sha256"],
                    "A paced Name receipt differs from its delivered choice.")
            for key in hashes:
                checksum(item[key])


def projection(clock, metadata, trust, caption=None):
    """Return the existing player's native-span/decoded-duration vocabulary."""
    require(clock.get('format')==FORMAT and clock.get('delivery_profile')==PROFILE,
            'Unsupported typed delivery profile.')
    require(clock.get('rate')==44100 and clock.get('scheduling_clock')=='decoded_ogg_pcm',
            'The delivery needs its measured decoded sample clock.')
    require(clock.get('native_manifest_sha256')==trust['source_native_manifest_sha256']
            and clock.get('candidate_sha256')==trust['source_candidate_manifest_sha256'],
            'The paced contract belongs to a different admitted native source.')
    require(metadata.get('source_id')==trust['source_id'] and metadata.get('npc_id')==trust['npc_id']
            and metadata.get('sample_rate')==44100,'The paced selection owner changed.')
    source=metadata['source_id'];prefix,_,rest=source.partition(':')
    giver_type = metadata.get('giver_type')
    require(giver_type in {'Creature', 'GameObject', 'Item'}, 'The admitted source needs its explicit giver type.')
    require(metadata.get('locale')=='en-US','This paced companion currently requires its admitted English locale.')
    require(prefix in DELIVERY_FOLDERS, 'Unsupported paced dialogue event.')
    kind = {"q": "quest", "g": "gossip", "f": "followup"}[prefix]
    match=re.fullmatch(r'q:([1-9][0-9]*):(accept|progress|complete)(?::([mf]))?',source) if kind=='quest' else None
    require(kind!='quest' or match is not None,'Unknown native quest event or branch.')
    event=match[2] if match else None
    branch=match[3] if match else None
    if giver_type == 'Item':
        require(kind == 'quest' and event == 'accept'
                and type(metadata['npc_id']) is int and metadata['npc_id'] > 0,
                'An item giver needs a positive item ID and a quest acceptance.')
        require(metadata['spoken_key'] == (f'{branch}-' if branch else '') + f'{match[1]}-accept',
                'The item starter key differs from its native quest or branch.')
    native_followup = {}
    require(kind!='quest' or event in {'accept','progress','complete'},'Unknown native quest event.')
    if kind == "followup":
        followup = re.fullmatch(r"f:([1-9][0-9]*):([a-z0-9-]+)(?::([mf]))?", source)
        require(followup and metadata["giver_type"] == "Creature", "Unknown native follow-up speaker or identity.")
        broadcast_id, voice, branch = followup.groups()
        stem = (branch + "-" if branch else "") + broadcast_id + "-" + voice
        key = metadata["spoken_key"]
        stock = metadata.get("original_spoken")
        if stock is not None:
            stock_path = stock.get("path") if isinstance(stock, dict) else None
            # Joint-speaker stems add a suffix to the exact stock stem.
            require(isinstance(key, str) and re.fullmatch(re.escape(stem) + r"(?:-[a-z0-9]+)*", key)
                    and isinstance(stock_path, str) and
                    re.fullmatch(r"[A-Za-z0-9_]+/generated/sounds/followup/" + re.escape(key) + r"[.]ogg", stock_path),
                    "This follow-up needs its bound stock recording.")
            checksum(stock.get("sha256"))
        else:
            model_id = re.fullmatch(r"model-([1-9][0-9]*)", voice)
            model = metadata.get("followup_model")
            require(key == stem and model_id and isinstance(model, dict)
                    and model.get("model_data_id") == int(model_id[1])
                    and type(model.get("file_data_id")) is int and model["file_data_id"] > 0,
                    "A follow-up without stock needs its bound client model.")
            checksum(model.get("model_table_sha256"))
            native_followup = dict(broadcast_id=int(broadcast_id), model_file_id=model["file_data_id"])
    native=derived=0;sequence=[]
    require(len(clock['sequence'])==len(metadata['occurrences']),'A paced source occurrence is missing.')
    for index,(part,owner) in enumerate(zip(clock['sequence'],metadata['occurrences'],strict=True),1):
        require(part['kind'] in KINDS and owner['kind']==part['kind'] and owner['sequence_index_1based']==index,
                'A paced occurrence changed its source order or kind.')
        a,b=part['native_span'];lo,hi=part['derived_span']
        require(all(type(n) is int for n in (a,b,lo,hi)) and a==native and lo==derived and b>a and hi>lo
                and owner['source_samples']==[a,b],'Paced source/delivery spans are not consecutive.')
        native,derived=b,hi
        fixed=part['kind']=='fixed';keys={'fixed'} if fixed else set(owner['choice_keys'])
        require(set(part['choices'])==keys and part['fallback']==('fixed' if fixed else owner['fallback']),
                'The paced choices or contextual fallback changed.')
        entry=dict(kind=part['kind'],source_samples=[a,b])
        choices={}
        for value,clip in part['choices'].items():
            filename=clip['file']
            require(isinstance(filename,str) and re.fullmatch('[A-Za-z0-9_.-]+[.]ogg',filename)
                    and Path(filename).name==filename,'A delivery clip leaves its package.')
            require(clip['clock']=='decoded_ogg_pcm' and type(clip['samples']) is int and clip['samples']>0,
                    'A paced clip has an invalid decoded duration.')
            checksum(clip['sha256']);checksum(clip['unit'])
            choices[value]={key:clip[key] for key in ('file','sha256','samples')}
            choices[value].update(saved_cuts(clip))
            if fixed:require(clip['samples']==hi-lo,'A fixed clip differs from the measured delivery span.')
        if fixed:entry['clip']=choices['fixed']
        else:
            entry.update(occurrence=owner['occurrence'],delimiter=owner['delimiter'],fallback=owner['fallback'],
                         expected_values=owner['expected_values'],choices=choices)
            if part['kind']=='gender':
                values=owner['value_by_player_sex']
                require(set(values)=={'2','3'} and set(values.values())==keys,'The source gender branches changed.')
                entry['value_by_player_sex']=values
        sequence.append(entry)
    require(native==metadata['source_samples'],'The paced route does not cover the complete native source.')
    validate_occurrences(sequence, complete_names=True)
    verify_name_receipts(sequence, metadata.get("personalized_source_extension", {}).get("name_receipts", {}))
    result=dict(source_id=source,npc_id=metadata['npc_id'],npc=metadata['npc'],locale=metadata['locale'],
        spoken_key=metadata['spoken_key'],original_text=metadata['original_text'],original_spoken=metadata['original_spoken'],
        source_samples=native,sample_rate=44100,source_wave_sha256=metadata['source_wave_sha256'],
        source_native_manifest_sha256=trust['source_native_manifest_sha256'],
        paced_source=dict(native_manifest_sha256=trust['source_native_manifest_sha256']),
        native_route=dict(kind=kind,key=metadata['spoken_key'],npc_id=metadata['npc_id'],
                          original_text=metadata['original_text'],giver_type=giver_type,
                          event=event, **native_followup),
        npc_aliases=metadata.get('npc_aliases',[]),sequence=sequence,
        delivery_clock_format=FORMAT,delivery_profile=PROFILE,delivery_contract_sha256=trust['clock_contract_sha256'],
        caption_policy=clock['caption_policy'],delivery_native_selection_sha256=trust['native_selection_metadata_sha256'])
    if kind == 'quest' and giver_type in {'Item', 'GameObject'}:
        result['native_route']['quest_id'] = int(match[1])
    if 'verified_quest_stock_variant' in metadata:
        verified = metadata['verified_quest_stock_variant']
        require(kind == 'quest' and giver_type == 'Creature' and metadata['original_spoken'] is None
                and isinstance(verified, dict) and set(verified) == {'path', 'sha256'}
                and isinstance(verified['path'], str),
                'A verified quest stock variant needs its native creature and signed recording.')
        stock_key = f'{branch + "-" if branch else ""}{match[1]}-{event}'
        require(metadata['spoken_key'] == stock_key and re.fullmatch(
            rf'SpokenQuestsAudio(?:Alliance|Horde|Shared)/generated/sounds/quests/{stock_key}-[a-z0-9-]+\.ogg',
            verified['path']), 'The verified stock variant belongs to a different quest moment.')
        checksum(verified['sha256'])
        result['native_route']['verified_quest_stock_variant'] = verified
    if 'verified_quest_giver_aliases' in metadata:
        givers = metadata['verified_quest_giver_aliases']
        require(kind == 'quest' and giver_type == 'Creature' and isinstance(givers, list) and givers
                and all(type(giver) is int and giver > 0 and giver != metadata['npc_id'] for giver in givers)
                and givers == sorted(set(givers)), 'Invalid verified creature quest aliases.')
        text_sha = digest(metadata['original_text'].encode())
        declared = {alias.get('npc_id') for alias in result['npc_aliases']
                    if alias.get('source_id') == source and alias.get('source_text_sha256', text_sha) == text_sha}
        require(set(givers) <= declared, 'A verified quest giver alias differs from its admitted source.')
        result['native_route']['verified_quest_giver_aliases'] = givers
    if caption is not None:
        require(caption['quest_text']==metadata['original_text'] and isinstance(caption['text'],str),
                'The source caption belongs to another dialogue.')
        caption_event = CAPTION_EVENTS[event if kind == "quest" else kind]
        require(caption['event'] == caption_event,
                'The caption event differs from the admitted native stage.')
        starts=caption['word_samples']
        require(all(type(x) is int and 0<=x<native for x in starts) and starts==sorted(starts)
                and len(starts)==len(caption['text'].split()),'The native caption sample clock is invalid.')
        result['reconstructed_caption']=caption
        used=set();previous=0
        for binding in caption.get('bindings',[]):
            ordinal=binding['sequence_index_1based'];first=binding['first'];last=binding['last']
            require(type(ordinal) is int and 1<=ordinal<=len(sequence) and ordinal not in used,
                    'A caption occurrence was merged or duplicated.')
            part=sequence[ordinal-1];used.add(ordinal)
            require(part['kind']!='fixed' and type(first) is int and type(last) is int and 1<=first<=last<=len(starts)
                    and binding['source']==' '.join(caption['text'].split()[first-1:last])
                    and set(binding['choices'])==set(part['choices'])
                    and all(isinstance(v,str) and v.strip() and len(v)<500 for v in binding['choices'].values()),
                    'A caption selection differs from its exact source occurrence.')
            require(first>previous,'Caption occurrences overlap or changed order.')
            previous=last
            part['caption_binding']={key:binding[key] for key in ('first','last','source','choices')}
            if 'delivered_word_samples' in binding:
                measured = binding['delivered_word_samples']
                require(isinstance(measured, dict) and measured and set(measured) <= set(part['choices']),
                        'Measured captions need a delivered choice.')
                for value, samples in measured.items():
                    require(isinstance(samples, list) and len(samples) == len(binding['choices'][value].split())
                            and all(type(sample) is int and 0 <= sample < part['choices'][value]['samples']
                                    for sample in samples)
                            and samples == sorted(samples),
                            'Measured caption starts must follow the words within the selected Ogg.')
                part['caption_binding']['delivered_word_samples'] = measured
        require(used=={i for i,p in enumerate(sequence,1) if p['kind']!='fixed'},
                'A typed caption needs every delimiter occurrence.')
    if branch:
        result['native_character_filters']={'sexes':['male' if branch=='m' else 'female']}
    return result


def admit(folder, clock_raw, metadata_raw, trust, caption_raw=None, *, verify_pcm=True, decoded_clocks=None):
    """Only hashes from the caller's externally pinned index establish trust."""
    require(digest(clock_raw)==checksum(trust.get('clock_contract_sha256'))
            and digest(metadata_raw)==checksum(trust.get('native_selection_metadata_sha256')),
            'The externally trusted delivery contract or source metadata changed.')
    checksum(trust['source_native_manifest_sha256']);checksum(trust['source_candidate_manifest_sha256'])
    require((caption_raw is not None)==bool(trust.get('caption_sha256')),
            'A sealed caption cannot be omitted or supplied without its external identity.')
    if caption_raw is not None:
        require(digest(caption_raw)==checksum(trust.get('caption_sha256')),'The externally trusted caption changed.')
    clock=json.loads(clock_raw);metadata=json.loads(metadata_raw)
    caption=json.loads(caption_raw) if caption_raw is not None else None
    expected=trust.get('clock_format',FORMAT)
    require(isinstance(clock,dict) and clock.get('format')==expected,'The delivery clock does not match its bundle format.')
    if expected==EXPANDED_FORMAT:
        from lite_expanded_context import project
        try:
            manifest=project(clock,metadata,trust,caption)
        except (KeyError,TypeError,IndexError,AttributeError) as error:
            raise ValueError('The expanded delivery has malformed source ownership or caption data.') from error
    else:
        manifest=projection(clock,metadata,trust,caption)
    files=set()
    for part in manifest['sequence']:
        for clip in ([part['clip']] if part['kind']=='fixed' else part['choices'].values()):
            path = Path(folder) / clip["file"]
            if "store_clip_paths" in trust:
                from lite_store_playback import stored_clip_path
                path = stored_clip_path(folder, trust, clip["file"])
            require(not path.is_symlink(), "A paced clip cannot be a link.")
            require(path.is_file(), 'A sealed delivered Ogg is missing.')
            require(file_hash(path)==clip['sha256'],'A sealed delivered Ogg changed.')
            clock_identity = (clip["sha256"], clip["samples"])
            if verify_pcm and (decoded_clocks is None or clock_identity not in decoded_clocks):
                with path.open('rb') as stream, sf.SoundFile(stream) as sound:
                    require((sound.format,sound.subtype,sound.samplerate,sound.channels,sound.frames)==
                            ('OGG','VORBIS',44100,1,clip['samples']),'A paced Ogg has the wrong decoded clock.')
                    count=0
                    while len(block:=sound.read(32768,dtype='float32')):count+=len(block)
                    require(count==clip['samples'],'The decoded Ogg ends at another sample.')
                if decoded_clocks is not None:
                    decoded_clocks.add(clock_identity)
            files.add(clip['file'])
    return manifest,files


def validate_projection(folder, manifest, trusted, *, verify_pcm=True, decoded_clocks=None):
    require(isinstance(trusted,dict),'A typed delivery needs its externally admitted seal.')
    caption = None
    if trusted.get('caption_sha256'):
        path = Path(folder) / 'caption.json'
        require(path.is_file() and not path.is_symlink(), 'The sealed caption file is missing or invalid.')
        caption = path.read_bytes()
    expected,files=admit(folder,(Path(folder)/'clock-contract.json').read_bytes(),
                         (Path(folder)/'native-selection-metadata.json').read_bytes(),trusted,caption,
                         verify_pcm=verify_pcm, decoded_clocks=decoded_clocks)
    require(canonical(manifest)==canonical(expected),'The staged paced projection changed after admission.')
    return files|{'clock-contract.json','native-selection-metadata.json'}|({'caption.json'} if caption else set())


def name_article_changed(before, after, value):
    if not isinstance(after, str):
        return False
    old, new = before.split(), after.split()
    if len(old) != len(new):
        return False
    changes = [index for index, (a, b) in enumerate(zip(old, new, strict=True)) if a != b]
    if len(changes) != 1 or changes[0] + 1 >= len(old):
        return False
    index = changes[0]
    selected = re.sub(r"^\W+|\W+$", "", old[index + 1]).casefold()
    return selected == value and {old[index].casefold(), new[index].casefold()} == {"a", "an"}


def additive_caption_extension(previous, incoming):
    """Allow a seal to add measured captions to the same admitted audio."""
    _, old, old_seal, _ = previous
    _, new, new_seal, _ = incoming
    if old.get("delivery_clock_format") != FORMAT or new.get("delivery_clock_format") != FORMAT:
        return False
    exact_audio_keys = ("clock_contract_sha256", "native_selection_metadata_sha256",
                        "source_native_manifest_sha256", "source_candidate_manifest_sha256")
    if any(old_seal[key] != new_seal[key] for key in exact_audio_keys):
        return False

    audio_fields = {"reconstructed_caption", "sequence"}
    if ({key: value for key, value in old.items() if key not in audio_fields}
            != {key: value for key, value in new.items() if key not in audio_fields}):
        return False
    if len(old["sequence"]) != len(new["sequence"]):
        return False
    for before, after in zip(old["sequence"], new["sequence"], strict=True):
        if ({key: value for key, value in before.items() if key != "caption_binding"}
                != {key: value for key, value in after.items() if key != "caption_binding"}):
            return False
    return adds_caption_samples(old.get("reconstructed_caption"), new.get("reconstructed_caption"))


def adds_caption_samples(old_caption, new_caption):
    field = "delivered_word_samples"
    if not new_caption or not any(binding.get(field) for binding in new_caption["bindings"]):
        return False
    if old_caption is None:
        return True

    if ({key: value for key, value in old_caption.items() if key != "bindings"}
            != {key: value for key, value in new_caption.items() if key != "bindings"}):
        return False
    if len(old_caption["bindings"]) != len(new_caption["bindings"]):
        return False
    added = False
    for before, after in zip(old_caption["bindings"], new_caption["bindings"], strict=True):
        if ({key: value for key, value in before.items() if key != field}
                != {key: value for key, value in after.items() if key != field}):
            return False
        old_samples, new_samples = before.get(field, {}), after.get(field, {})
        if any(new_samples.get(key) != value for key, value in old_samples.items()):
            return False
        added |= len(new_samples) > len(old_samples)
    return added


def additive_quest_alias_extension(previous, incoming):
    """Permit audited giver additions to a byte-identical quest delivery."""
    old_folder, old, old_seal, _ = previous
    new_folder, new, new_seal, _ = incoming
    field = 'verified_quest_giver_aliases'
    if (old.get('delivery_clock_format') != FORMAT or new.get('delivery_clock_format') != FORMAT
            or any(old_seal.get(key) != new_seal.get(key) for key in (
                'clock_contract_sha256', 'caption_sha256', 'source_native_manifest_sha256',
                'source_candidate_manifest_sha256'))):
        return False
    before = read(old_folder / 'native-selection-metadata.json')
    after = read(new_folder / 'native-selection-metadata.json')
    old_givers = set(before.pop(field, []))
    new_givers = set(after.pop(field, []))
    if before != after or not old_givers < new_givers:
        return False
    before, after = deepcopy(old), deepcopy(new)
    for manifest in (before, after):
        manifest['native_route'].pop(field, None)
        manifest.pop('delivery_native_selection_sha256', None)
    return before == after


def gossip_alias_correction(existing, incoming):
    """Allow a portable batch to move aliases while keeping every owner's audio."""
    existing_routes = {dialogue_source_tuple(manifest): entry for entry in existing
                       if (manifest := entry[1])['native_route']['kind'] == 'gossip'}
    assignments = [{}, {}]
    keys = set()
    for after in incoming:
        folder, manifest, seal, _ = after
        key = dialogue_source_tuple(manifest)
        require(key not in keys and key in existing_routes
                and manifest['native_route']['kind'] == 'gossip',
                'A gossip alias correction needs its existing canonical routes.')
        keys.add(key)
        old_folder, old, old_seal, _ = existing_routes[key]
        require(old['delivery_clock_format'] == FORMAT and manifest['delivery_clock_format'] == FORMAT
                and old_seal.get('caption_sha256') == seal.get('caption_sha256'),
                'A gossip alias correction cannot change the delivery format or caption.')
        old_meta, new_meta = (read(path / 'native-selection-metadata.json') for path in (old_folder, folder))
        old_clock, new_clock = (read(path / 'clock-contract.json') for path in (old_folder, folder))
        for metadata, owner, known in zip((old_meta, new_meta), (old, manifest), assignments, strict=True):
            for alias in metadata.pop('npc_aliases'):
                identity = (owner['source_id'], owner['locale'], alias['npc_id'])
                require(alias['source_id'] == owner['source_id'] and identity not in known
                        and type(alias['npc_id']) is int and alias['npc_id'] != owner['npc_id'],
                        'A gossip alias correction has duplicate or unrelated aliases.')
                known[identity] = (canonical(alias), owner['original_text'])
            metadata.pop('native_manifest_sha256')
        for clock in (old_clock, new_clock):
            for member in ('native_manifest_sha256', 'candidate_sha256', 'render_fingerprint'):
                clock.pop(member)
        require(old_clock == new_clock and old_meta == new_meta,
                'A gossip alias correction cannot replace audio, cuts, names or source metadata.')
    require(len(keys) >= 2 and assignments[0] == assignments[1],
            'A gossip alias correction must move the same aliases together.')
    return keys


def fallback_base_identity(seal):
    """Identify the exact admitted recipe independently of its ZIP directory."""
    fields = ('source_id', 'npc_id', 'source_native_manifest_sha256', 'source_candidate_manifest_sha256',
              'clock_contract_sha256', 'native_selection_metadata_sha256', 'caption_sha256')
    return digest(canonical([seal.get(field) for field in fields]))


def fallback_name_slots(manifest):
    slots = []
    for part in manifest['sequence']:
        if part['kind'] == 'fixed':
            continue
        if part['kind'] != 'name' or part['fallback'] != 'adventurer' or set(part['choices']) != {'adventurer'}:
            return []
        slots.append((part['delimiter'], part['occurrence'], part['expected_values']))
    return slots


def fallback_selection_layout(manifest):
    """Compare shipped selections while rejecting any personalized Name."""
    slots = []
    for part in manifest['sequence']:
        kind = part['kind']
        if kind == 'fixed':
            continue
        choices = set(part['choices'])
        fallback = part['fallback']
        if kind == 'name' and (fallback != 'adventurer' or choices != {'adventurer'}):
            return []
        slots.append((kind, part['delimiter'], part['occurrence'], fallback, sorted(choices),
                      sorted(part['expected_values']), part.get('value_by_player_sex')))
    return slots


def signed_fallback_recording(manifest):
    """Validate the stock route of a newly recovered quest or gossip recording."""
    route = manifest['native_route']
    stock = manifest['original_spoken']
    if not isinstance(stock, dict) or set(stock) != {'path', 'sha256'} or not isinstance(stock['path'], str):
        return False
    key = manifest['spoken_key']
    if route['kind'] == 'quest':
        _, quest, event, *branch = manifest['source_id'].split(':')
        stem = (branch[0] + '-' if branch else '') + quest + '-' + event
        folder = r'SpokenQuestsAudio(?:Alliance|Horde|Shared)/generated/sounds/quests/'
    elif route['kind'] == 'gossip':
        stem = manifest['source_id'].partition(':')[2]
        folder = 'SpokenQuestsAudioGossip/generated/sounds/gossip/'
    else:
        return False
    return (re.fullmatch(re.escape(stem) + r'(?:-[a-z0-9]+)*', key) is not None
            and re.fullmatch(folder + re.escape(key) + r'[.]ogg', stock['path']) is not None
            and isinstance(stock['sha256'], str) and re.fullmatch('[0-9a-f]{64}', stock['sha256']) is not None)


def reconstructed_object_fallback(folder, manifest):
    """Identify the complete base clock shared by a reconstructed object recipe."""
    route = manifest['native_route']
    if (manifest['original_spoken'] is not None or route['kind'] != 'quest'
            or route['giver_type'] != 'GameObject' or not fallback_name_slots(manifest)):
        return None
    clock = read(folder / 'clock-contract.json')
    units = {choice.get('unit') for part in clock['sequence'] for choice in part['choices'].values()}
    if len(units) != 1 or None in units:
        return None
    if any(part['choices'][part['fallback']]['samples'] != part['derived_span'][1] - part['derived_span'][0]
           for part in clock['sequence']):
        return None
    return (manifest['source_wave_sha256'], manifest['source_samples'], clock['sequence'][-1]['derived_span'][1],
            units.pop())


def delimiter_only_resegmentation(before, after):
    """Check that a reconstructed Name cut returns the neighboring words to the fixed audio."""
    old = [part for part in before['sequence'] if part['kind'] == 'name']
    new = [part for part in after['sequence'] if part['kind'] == 'name']
    return all(
        prior['source_samples'][0] <= current['source_samples'][0] < current['source_samples'][1]
        <= prior['source_samples'][1] and prior['source_samples'] != current['source_samples']
        and re.fullmatch(r'Adventurer[.!?…]*', current.get('caption_binding', {}).get('source', ''), flags=re.I)
        and current['caption_binding']['choices'] == {'adventurer': current['caption_binding']['source']}
        and len(prior.get('caption_binding', {}).get('source', '').split()) > 1
        for prior, current in zip(old, new, strict=True)
    )


def same_fixed_name_clock(before_folder, after_folder):
    """Verify that a measured fallback refresh owns only the existing Name spans."""
    before = read(before_folder / 'clock-contract.json')
    after = read(after_folder / 'clock-contract.json')
    before_parts = before.pop('sequence')
    after_parts = after.pop('sequence')
    before.pop('paired_reference_rehearsal', None)
    after.pop('paired_reference_rehearsal')
    if before != after or len(before_parts) != len(after_parts):
        return False
    changed = False
    for old, new in zip(before_parts, after_parts, strict=True):
        if old == new:
            continue
        if old['kind'] != 'name' or {**old, 'choices': new['choices']} != new:
            return False
        changed = True
    if not changed:
        return False
    captions = []
    for folder in (before_folder, after_folder):
        caption = read(folder / 'caption.json')
        for binding in caption['bindings']:
            binding.pop('delivered_word_samples', None)
        captions.append(caption)
    return captions[0] == captions[1]


def fallback_base_replacements(declared, existing, incoming):
    """Resolve a pinned fallback-only predecessor after both recipes are admitted."""
    require(isinstance(declared, dict) and declared, 'Playback base replacements need exact predecessor identities.')
    proposed = {seal['directory']: (folder, manifest, seal) for folder, manifest, seal, _ in incoming}
    known = {fallback_base_identity(seal): (folder, manifest) for folder, manifest, seal, _ in existing}
    approved = {}
    used = set()
    for directory, predecessor in declared.items():
        identities = [predecessor] if isinstance(predecessor, str) else predecessor
        require(directory in proposed and isinstance(identities, list) and identities,
                'A playback base replacement has an unknown directory or missing predecessor.')
        for identity in identities:
            checksum(identity)
            require(identity not in used, 'A playback base replacement repeated a predecessor identity.')
            used.add(identity)
        folder, after, seal = proposed[directory]
        slots = fallback_selection_layout(after)
        after_metadata = read(folder / 'native-selection-metadata.json')
        reconstructed = reconstructed_object_fallback(folder, after)
        refreshed = 'paired_reference_rehearsal' in read(folder / 'clock-contract.json')
        require(not refreshed or fallback_name_slots(after),
                'A Name refresh must contain only the existing Name occurrences.')
        require(after['delivery_clock_format'] == FORMAT and slots
                and (signed_fallback_recording(after) or reconstructed or refreshed),
                'A playback base replacement needs an admitted recipe without personalized Names.')
        require('personalized_source_extension' not in after_metadata,
                'A playback base replacement must preserve the installed personal Names.')
        for identity in identities:
            require(identity != fallback_base_identity(seal), 'A playback base replacement must change its exact recipe.')
            if identity not in known:
                continue
            before_folder, before = known[identity]
            if refreshed:
                require(before['delivery_clock_format'] == FORMAT and fallback_name_slots(after)
                        and fallback_selection_layout(before) == slots
                        and before['delivery_native_selection_sha256'] == after['delivery_native_selection_sha256']
                        and same_fixed_name_clock(before_folder, folder),
                        'A Name refresh changed its source clock, fixed clips or caption wording.')
                approved.setdefault(directory, set()).add(identity)
                continue
            before_metadata = read(before_folder / 'native-selection-metadata.json')
            native_before, native_after = ({key: value for key, value in recipe['native_route'].items() if key != 'key'}
                                           for recipe in (before, after))
            require(before['delivery_clock_format'] == FORMAT and before['original_spoken'] is None
                    and 'personalized_source_extension' not in before_metadata
                    and dialogue_source_tuple(before) == dialogue_source_tuple(after)
                    and quest_wording_key(before) == quest_wording_key(after)
                    and native_before == native_after and before['npc'] == after['npc']
                    and before['npc_aliases'] == after['npc_aliases']
                    and fallback_selection_layout(before) == slots,
                    'A playback base replacement changed the admitted source, aliases, or delimiter selections.')
            if reconstructed:
                require(reconstructed_object_fallback(before_folder, before) == reconstructed
                        and delimiter_only_resegmentation(before, after),
                        'The reconstructed object replacement must isolate the Name on the existing source clock.')
            approved.setdefault(directory, set()).add(identity)
    return approved


def preserve_predecessor_name(proof, installed, incoming, prior_hashes):
    """Recognize a verified Name extension of the exact replaced fallback."""
    folder, manifest, _, _ = installed
    extension = read(folder / 'native-selection-metadata.json').get('personalized_source_extension')
    if (not extension or proof['manifest_sha256'] not in prior_hashes
            or extension.get('root_native_manifest_sha256') != proof['native_manifest_sha256']
            or digest(canonical(extension)) != manifest['source_native_manifest_sha256']):
        return False
    kinds = {part['kind'] for part in manifest['sequence']}
    return (manifest['delivery_clock_format'] == FORMAT
            and manifest['native_route']['kind'] in {'quest', 'followup'}
            and 'name' in kinds and kinds <= KINDS
            and manifest['original_text'] == incoming['original_text']
            and manifest['native_route']['giver_type'] == incoming['native_route']['giver_type'])


def additive_name_extension(previous, incoming):
    """Accept a sealed Name addition or an exact a/an correction."""
    old_folder,old_manifest,_,_=previous
    new_folder,new_manifest,_,_=incoming
    if old_manifest['delivery_clock_format'] == new_manifest['delivery_clock_format'] == EXPANDED_FORMAT:
        from lite_expanded_name_addition import additive
        return additive(previous, incoming)
    if (old_manifest['delivery_clock_format'] != FORMAT or new_manifest['delivery_clock_format'] != FORMAT):
        return False
    old_meta=read(old_folder/'native-selection-metadata.json')
    new_meta=read(new_folder/'native-selection-metadata.json')
    lineage=new_meta.get('personalized_source_extension',{})
    previous_receipts=old_meta.get("personalized_source_extension", {}).get("name_receipts", {})
    receipts=lineage.get("name_receipts", {})
    if lineage.get('base_native_manifest_sha256')!=old_manifest['source_native_manifest_sha256']:
        return False
    # The externally pinned extension also binds the real native named recipe,
    # raw return and unchanged request identity. These are provenance hashes,
    # never paths read by the Ogg-only consumer.
    for key in ('actual_name_manifest_sha256','raw_response_sha256','request_sha256'):
        if not isinstance(lineage.get(key),str) or not re.fullmatch('[0-9a-f]{64}',lineage[key]):return False
    ignored={'native_manifest_sha256','personalized_source_extension','occurrences'}
    if {k:v for k,v in old_meta.items() if k not in ignored}!={k:v for k,v in new_meta.items() if k not in ignored}:return False
    old_clock=read(old_folder/'clock-contract.json');new_clock=read(new_folder/'clock-contract.json')
    ignored={'native_manifest_sha256','candidate_sha256','render_fingerprint','sequence'}
    if {k:v for k,v in old_clock.items() if k not in ignored}!={k:v for k,v in new_clock.items() if k not in ignored}:return False
    if len(old_clock['sequence'])!=len(new_clock['sequence']):return False
    added=False
    repairs = []
    repaired_captions = set()
    new_captions = set()
    for index, (before, after, owner, new_owner) in enumerate(zip(
            old_clock['sequence'], new_clock['sequence'], old_meta['occurrences'], new_meta['occurrences'],
            strict=True), 1):
        if before['kind']!='name':
            if before!=after or owner!=new_owner:return False
            continue
        if {k:v for k,v in before.items() if k!='choices'}!={k:v for k,v in after.items() if k!='choices'}:return False
        for value, clip in before["choices"].items():
            replacement = after["choices"].get(value)
            if replacement == clip:
                continue
            if (not replacement or value == before["fallback"] or replacement["file"] != clip["file"]
                    or replacement["sha256"] == clip["sha256"]):
                return False
            repairs.append(dict(occurrence=owner["occurrence"], value=value,
                                old_sha256=clip["sha256"], sha256=replacement["sha256"]))
            repaired_captions.add((index, value))
        keys=set(after['choices'])-set(before['choices']);added|=bool(keys)
        new_captions.update((index, key) for key in keys)
        if {k:v for k,v in owner.items() if k not in {'choice_keys','expected_values'}}!={k:v for k,v in new_owner.items() if k not in {'choice_keys','expected_values'}}:return False
        if not set(owner['choice_keys'])<=set(new_owner['choice_keys']):return False
        if not set(owner['expected_values'])<=set(new_owner['expected_values']):return False
        if {v.casefold() for v in new_owner['expected_values']}!=set(new_owner['choice_keys']):return False
    if not (added or repairs) or lineage.get("article_repairs", []) != repairs:
        return False
    if len({repair["value"] for repair in repairs}) > 1:
        return False
    repaired_choices = {(repair["value"], repair["occurrence"]) for repair in repairs}
    current_receipts = {(value, item["occurrence"]): item for value, items in receipts.items() for item in items}
    if not repaired_choices <= current_receipts.keys():
        return False
    for value, items in previous_receipts.items():
        for item in items:
            key = (value, item["occurrence"])
            updated = current_receipts.get(key)
            if key in repaired_choices:
                if (updated["request_sha256"] == item["request_sha256"]
                        or updated["donor_sha256"] == item["donor_sha256"]):
                    return False
            elif updated != item:
                return False
    old_caption=old_manifest.get('reconstructed_caption');new_caption=new_manifest.get('reconstructed_caption')
    if not old_caption or not new_caption:return False
    if {k:v for k,v in old_caption.items() if k!='bindings'}!={k:v for k,v in new_caption.items() if k!='bindings'}:return False
    if len(old_caption['bindings'])!=len(new_caption['bindings']):return False
    verified_captions = set()
    measured_field = 'delivered_word_samples'
    for before,after in zip(old_caption['bindings'],new_caption['bindings'],strict=True):
        mutable = {'choices', measured_field}
        if {k:v for k,v in before.items() if k not in mutable}!={k:v for k,v in after.items() if k not in mutable}:return False
        index = before['sequence_index_1based']
        # A newly admitted clip owns its timing. A Name extension cannot
        # replace or fill in starts on a clip it did not install or repair.
        old_starts, new_starts = before.get(measured_field, {}), after.get(measured_field, {})
        for value in old_starts.keys() | new_starts.keys():
            if (old_starts.get(value) != new_starts.get(value)
                    and (index, value) not in new_captions | repaired_captions):
                return False
        for value, text in before["choices"].items():
            replacement = after["choices"].get(value)
            if replacement == text:
                continue
            owner = (index, value)
            if owner not in repaired_captions or not name_article_changed(text, replacement, value):
                return False
            verified_captions.add(owner)
    return verified_captions == repaired_captions


def ordinary_name_base_caption(incoming, installed, *, allow_article_predecessor=False):
    """Recover the prior recipe's caption by removing verified later Name choices."""
    base_folder, base_manifest, _, _ = incoming
    active_folder, active_manifest, _, _ = installed
    if base_manifest["delivery_clock_format"] != FORMAT or active_manifest["delivery_clock_format"] != FORMAT:
        return None

    base_meta, active_meta = (read(folder / "native-selection-metadata.json")
                              for folder in (base_folder, active_folder))
    base_lineage = base_meta.pop("personalized_source_extension", {})
    active_lineage = active_meta.pop("personalized_source_extension", {})
    root_id = active_lineage.get("root_native_manifest_sha256")
    base_id = base_manifest["source_native_manifest_sha256"]
    active_id = active_manifest["source_native_manifest_sha256"]
    if not root_id or digest(canonical(active_lineage)) != active_id:
        return None
    if (base_meta.pop("native_manifest_sha256", base_id) != base_id
            or active_meta.pop("native_manifest_sha256", active_id) != active_id):
        return None
    if base_id != root_id:
        if digest(canonical(base_lineage)) != base_id:
            return None
        if any(base_lineage.get(key) != active_lineage.get(key) for key in (
                "root_native_manifest_sha256", "source_contract_sha256", "generation_source_key",
                "signed_stock_binding")):
            return None
    # The current repair can attest to its immediate, verified predecessor.
    if (allow_article_predecessor and active_lineage.get("article_repairs")
            and additive_name_extension(incoming, installed)):
        return base_manifest.get("reconstructed_caption")
    base_receipts = base_lineage.get("name_receipts", {})
    active_receipts = active_lineage.get("name_receipts", {})
    if any(active_receipts.get(key) != receipt for key, receipt in base_receipts.items()):
        return None

    base_clock, active_clock = (read(folder / "clock-contract.json") for folder in (base_folder, active_folder))
    for clock in (base_clock, active_clock):
        for key in ("native_manifest_sha256", "candidate_sha256", "render_fingerprint"):
            clock.pop(key, None)
    active_caption = deepcopy(active_manifest.get("reconstructed_caption"))
    if (not base_manifest.get("reconstructed_caption") or not active_caption
            or len(base_clock["sequence"]) != len(active_clock["sequence"])):
        return None

    added = False
    bindings = {binding["sequence_index_1based"]: binding for binding in active_caption["bindings"]}
    for index, (before, after, owner) in enumerate(zip(
            base_clock["sequence"], active_clock["sequence"], active_meta["occurrences"], strict=True), 1):
        if before["kind"] != "name" or after["kind"] != "name" or owner["kind"] != "name":
            continue
        extra = after["choices"].keys() - before["choices"].keys()
        if not extra:
            continue
        binding = bindings.get(index)
        if binding is None or after["fallback"] in extra or not extra <= binding["choices"].keys():
            return None
        added = True
        for key in extra:
            del after["choices"][key]
            del binding["choices"][key]
            if "delivered_word_samples" in binding:
                binding["delivered_word_samples"].pop(key, None)
                if not binding["delivered_word_samples"]:
                    del binding["delivered_word_samples"]
        owner["choice_keys"] = [key for key in owner["choice_keys"] if key not in extra]
        owner["expected_values"] = [label for label in owner["expected_values"] if label.casefold() not in extra]
    if added and base_clock == active_clock and base_meta == active_meta:
        return active_caption
    return None


def retains_ordinary_names(incoming, installed):
    """Recognize a reimported base or an older managed paced delivery."""
    prior_caption = ordinary_name_base_caption(incoming, installed, allow_article_predecessor=True)
    return prior_caption is not None and incoming[1].get("reconstructed_caption") == prior_caption


def merge_measured_base_caption(incoming, installed, root):
    """Enrich a verified managed recipe with measurements from its matching base."""
    base_caption = incoming[1].get("reconstructed_caption")
    if not base_caption or not any(binding.get("delivered_word_samples") for binding in base_caption["bindings"]):
        return None
    prior_caption = ordinary_name_base_caption(incoming, installed)
    if prior_caption is None or not adds_caption_samples(prior_caption, base_caption):
        return None

    folder, manifest, seal, files = installed
    caption = deepcopy(manifest["reconstructed_caption"])
    for old, new in zip(caption["bindings"], base_caption["bindings"], strict=True):
        if starts := new.get("delivered_word_samples"):
            old.setdefault("delivered_word_samples", {}).update(starts)
    raw = canonical(caption)
    caption_sha = digest(raw)
    revised_seal = dict(seal, caption_sha256=caption_sha)
    merged_root = root.with_name(digest(canonical([root.name, caption_sha])))
    target = merged_root / Path(*folder.parts[-5:])
    for name in files - {"caption.json"}:
        cache_bytes(target / name, (folder / name).read_bytes())
    cache_bytes(target / "caption.json", raw)
    upgraded, _ = admit(target, (target / "clock-contract.json").read_bytes(),
                        (target / "native-selection-metadata.json").read_bytes(), revised_seal, raw)
    save(target / "dialogue.json", upgraded)
    return target, upgraded, revised_seal, files | {"caption.json"}


def import_and_package(app, data, status=None, *, progress=None, build_id=None):
    from lite_portable_io import hold
    work=app.folder/'spoken-player-names'
    work.mkdir(parents=True,exist_ok=True)
    with hold(work,'.paced-import.lock'):
        if build_id and data.get("addons_folder"):
            reused = reuse_startup_playback(app, data, build_id, progress)
            if reused is not None:
                return reused
        return _import_and_package(app, data, status, progress=progress, build_id=build_id)


def reuse_startup_playback(app, data, build_id, progress):
    """Recheck the same bundle, cached Ogg bytes and game placement."""
    from lite_name_automatic import addons_folder
    from lite_portable_inputs import archive_identity, checked_hash, import_progress
    from lite_portable_package import ARCHIVE, INDEX, install_names

    state = app.store.setting(SETTING, {})
    receipt = state.get("startup_verification", {})
    if (receipt.get("build") != build_id or state.get("archive_sha256") != data["sha256"]
            or state.get("input_sha256") != data["input_sha256"] or not state.get("seals")
            or receipt.get("seals_sha256") != digest(canonical(state["seals"]))
            or receipt.get("preserved_name_seals_sha256") != preserved_name_digest(state)):
        return None
    addons = addons_folder(data["addons_folder"])
    index_hash = state.get("placement_index_sha256")
    if receipt["addons"] != path_key(addons) or not index_hash:
        return None

    archive = filesystem_path(Path(data["path"]).resolve())
    identity = archive_identity(archive)
    require(checked_hash(archive, progress=progress, stage="checking playback archive") == data["sha256"]
            and archive_identity(archive) == identity, "The paced delivery archive changed.")
    try:
        if file_hash(addons / INDEX) != index_hash:
            return None
        seals = state["seals"]
        checked = {**seals, **state.get("preserved_name_seals", {})}
        for number, (name, seal) in enumerate(checked.items()):
            import_progress(progress, None, "checking installed dialogue audio", number, len(checked))
            folder = filesystem_path(name)
            validate_projection(folder, read(folder / "dialogue.json"), seal, verify_pcm=False)
        import_progress(progress, None, "checking game playback")
        package = app.folder / "spoken-player-names" / ARCHIVE
        changed = install_names(package, addons, expected_index=index_hash, check_index=True,
                                archive_sha256=receipt["package_sha256"])
    except (OSError, ValueError, KeyError, BadZipFile):
        # Upgrade or repair via ordinary admission when any local copy differs.
        return None
    return dict(packaged=len(seals), placement_changed=changed, reused_verified_playback=True)


def preserved_name_digest(state):
    names = state.get('preserved_name_seals')
    return digest(canonical(names)) if names else None


def prepare_addition(app,data,status,*,base_state=None):
    """Stage an additive Name recipe for the later placement transaction."""
    from lite_portable_io import hold
    work=app.folder/'spoken-player-names';work.mkdir(parents=True,exist_ok=True)
    with hold(work,'.paced-import.lock'):
        if base_state is not None:
            live=app.store.setting(SETTING,{}) or {}
            expected=base_state.get('_prepared_from_collection_sha256',base_state.get('collection_sha256'))
            require(live.get('collection_sha256')==expected,
                    'The paced collection changed during Name preparation. Retry to preserve its new routes.')
        return _import_and_package(app,data,status,commit=False,base_state=base_state)


def paced_bundle_members(members, packages):
    directories = {}
    for item in packages:
        directory = item["directory"]
        require(isinstance(directory, str) and re.fullmatch(r"[A-Za-z0-9_-]+", directory),
                "Invalid paced package directory.")
        require(directory not in directories, "A paced package directory was duplicated.")
        directories[directory] = {}
    documents = {"clock-contract.json", "native-selection-metadata.json", "caption.json"}
    for member in members:
        if member == "input.json":
            continue
        directory, _, filename = member.partition("/")
        require(directory in directories, "The paced input has unrelated or unadmitted members.")
        require(filename and Path(filename).name == filename and not filename.startswith("."),
                "A paced asset leaves its dialogue folder.")
        require(filename.endswith(".ogg") or filename in documents,
                "A paced input contains an unrelated asset.")
        directories[directory][filename] = member
    for files in directories.values():
        require({"clock-contract.json", "native-selection-metadata.json"} <= files.keys(),
                "A paced dialogue is missing its clock or selection metadata.")
    return directories


def _import_and_package(app, data, status, *, commit=True, base_state=None, progress=None, build_id=None):
    """Ordinary app operation: verify portable Oggs, stage, package and optionally place."""
    from lite_portable_inputs import checked_hash, import_progress, safe_members
    from lite_portable_package import ARCHIVE,package_names,install_names
    archive=filesystem_path(Path(data['path']).expanduser().absolute()).resolve()
    archive_sha = checked_hash(archive, progress=progress, stage='checking playback archive')
    require(archive_sha==checksum(data.get('sha256')),'The paced delivery archive changed.')
    members=safe_members(archive, ARCHIVE_BYTES_LIMIT, max_members=ARCHIVE_MEMBER_LIMIT, lazy=True)
    raw=members['input.json']
    require(digest(raw)==checksum(data.get('input_sha256')),'The external paced index identity is required.')
    index=json.loads(raw);require(index.get('format') in (BUNDLE,EXPANDED_BUNDLE) and isinstance(index.get('packages'),list)
                                 and 0<len(index['packages'])<=DIALOGUE_LIMIT,'Choose a typed paced delivery input.')
    if 'gossip_alias_correction' in index:
        require(index['gossip_alias_correction'] is True and commit and index['format'] == BUNDLE,
                'A gossip alias correction must be imported as a single playback collection.')
    if 'fallback_base_replacements' in index:
        require(commit and index['format'] == BUNDLE and 'gossip_alias_correction' not in index,
                'A playback base replacement must be imported as a single ordinary playback collection.')
    clock_format=EXPANDED_FORMAT if index["format"]==EXPANDED_BUNDLE else FORMAT
    root=filesystem_path(app.folder/'spoken-player-names/paced-packages'/digest(raw))
    directories = paced_bundle_members(members, index["packages"])
    incoming = []
    # The decoded clock is reusable only for these exact bytes in this operation.
    decoded_clocks = set()
    total = len(index['packages'])
    import_progress(progress, None, 'verifying playback', 0, total)
    for current, item in enumerate(index['packages'], 1):
        require("clock_format" not in item, "The delivery index contains reserved local seal metadata.")
        if status is not None:
            status('Checking paced dialogue ' + item['source_id'])
        local = {name: members[member] for name, member in directories[item["directory"]].items()}
        source_key=json.loads(local['native-selection-metadata.json'])['spoken_key']
        require(isinstance(source_key,str) and re.fullmatch('[A-Za-z0-9_.-]+',source_key),'Invalid native dialogue folder.')
        prefix = item['source_id'].partition(':')[0]
        require(prefix in DELIVERY_FOLDERS, 'Unsupported paced dialogue event.')
        clock_hash=checksum(item.get('clock_contract_sha256'))
        folder=root/('DMSpeaksSpokenNames-paced-'+clock_hash[:20])/'generated/sounds'/DELIVERY_FOLDERS[prefix]/source_key
        for name,content in local.items():
            cache_bytes(folder/name,content)
        caption=local.get('caption.json')
        seal=dict(item,clock_format=clock_format)
        manifest, used = admit(folder, local['clock-contract.json'], local['native-selection-metadata.json'],
                               seal, caption, decoded_clocks=decoded_clocks)
        require(set(local)==used|{'clock-contract.json','native-selection-metadata.json'}|({'caption.json'} if caption else set()),
                'A paced input contains an unreferenced asset.')
        save(folder/'dialogue.json',manifest)
        persisted_seal = seal if clock_format == EXPANDED_FORMAT else item
        incoming.append((folder, manifest, persisted_seal, set(local)))
        import_progress(progress, None, 'verifying playback', current, total)
    import_progress(progress, None, 'packaging playback')
    # Re-admit every previous seal before combining it with this import. The
    # same source cannot silently displace an earlier character/choice recipe.
    previous=base_state if base_state is not None else app.store.setting(SETTING,{}) or {}
    from lite_expanded_name_addition import FIELD as EXPANDED_NAME_ADDITION, retains_installed_names
    existing = []
    for name, seal in previous.get("seals", {}).items():
        folder = filesystem_path(name)
        manifest = read(folder / "dialogue.json")
        files = validate_projection(folder, manifest, seal, decoded_clocks=decoded_clocks)
        existing.append((folder, manifest, seal, files))
    corrected_aliases = gossip_alias_correction(existing, incoming) if index.get('gossip_alias_correction') else set()
    replacements = (fallback_base_replacements(index['fallback_base_replacements'], existing, incoming)
                    if 'fallback_base_replacements' in index else {})
    predecessor_manifests = index.get('fallback_predecessor_manifests', {})
    require(isinstance(predecessor_manifests, dict)
            and predecessor_manifests.keys() <= index.get('fallback_base_replacements', {}).keys(),
            'The fallback predecessor manifests need exact declared replacements.')
    for directory, proofs in predecessor_manifests.items():
        proofs = [proofs] if isinstance(proofs, dict) else proofs
        require(isinstance(proofs, list) and proofs, 'The fallback predecessor manifest proof is missing.')
        for proof in proofs:
            require(isinstance(proof, dict) and proof.keys() == {'manifest_sha256', 'native_manifest_sha256'},
                    'The fallback predecessor manifest has an invalid identity.')
            for value in proof.values():
                checksum(value)
        predecessor_manifests[directory] = proofs
    preserved_names = dict(previous.get('preserved_name_seals', {}))
    combined = {}
    manifest_hashes = {}
    prior_deliveries = {}
    saved_prior_deliveries = previous.get("prior_delivery_hashes", {})
    wordings = {}
    for _, manifest, _, _ in (*existing, *incoming):
        wordings.setdefault(dialogue_source_tuple(manifest), set()).add(quest_wording_key(manifest))
    for index, entry in enumerate((*existing, *incoming)):
        _, manifest, seal, _ = entry
        key = dialogue_source_tuple(manifest)
        sibling_wordings = wordings[key]
        if len(sibling_wordings) > 1 and None not in sibling_wordings:
            key = (*key, quest_wording_key(manifest))
        incoming_entry = index >= len(existing)
        manifest_hash = digest(canonical(manifest))
        if incoming_entry and EXPANDED_NAME_ADDITION in manifest:
            require(key in combined, 'Import the admitted expanded base before adding a Name.')
        if key in combined and manifest_hash != manifest_hashes[key]:
            predecessors = replacements.get(seal['directory'], ()) if incoming_entry else ()
            if incoming_entry and (manifest_hash in prior_deliveries[key]
                                   or retains_ordinary_names(entry, combined[key])
                                   or retains_installed_names(entry, combined[key])):
                prior_deliveries[key].add(manifest_hash)
                continue
            proofs = predecessor_manifests.get(seal['directory'], ()) if incoming_entry else ()
            if any(preserve_predecessor_name(proof, combined[key], manifest, prior_deliveries[key])
                   for proof in proofs):
                saved_folder, _, saved_seal, _ = combined[key]
                require(data.get('addons_folder'), 'Choose the WoW AddOns folder to keep the existing Name.')
                preserved_names[path_key(saved_folder)] = saved_seal
            elif not ((incoming_entry and (additive_caption_extension(combined[key], entry)
                                         or additive_quest_alias_extension(combined[key], entry)
                                         or key in corrected_aliases
                                         or fallback_base_identity(combined[key][2]) in predecessors))
                    or additive_name_extension(combined[key], entry)):
                merged = merge_measured_base_caption(entry, combined[key], root) if incoming_entry else None
                require(merged, "This source already has another admitted recipe; merge its character choices before replacing it.")
                entry = merged
                prior_deliveries[key].add(manifest_hash)
                manifest_hash = digest(canonical(entry[1]))
            prior_deliveries[key].add(manifest_hashes[key])
        elif key not in combined:
            prior_deliveries[key] = set() if incoming_entry else set(saved_prior_deliveries.get(manifest_hash, []))
        combined[key] = entry
        manifest_hashes[key] = manifest_hash
    active_identity = sorted(manifest_hashes.items())
    identity = digest(canonical([active_identity, preserved_names] if preserved_names else active_identity))
    prior_hashes = {manifest_hashes[key]: sorted(hashes)
                    for key, hashes in prior_deliveries.items() if hashes}
    state = dict(input_sha256=digest(raw), archive_sha256=data['sha256'],
                 collection_sha256=identity)
    if preserved_names:
        state['preserved_name_seals'] = preserved_names
    if prior_hashes:
        state['prior_delivery_hashes'] = prior_hashes
    if not commit:
        # Each admitted recipe already has its own immutable source directory.
        # Keep the selected paths so a new Name needs no second full library.
        state['seals'] = {path_key(folder): seal for folder, _, seal, _ in combined.values()}
        require(len(state['seals']) == len(combined), 'Two paced recipes share a source directory.')
        state['installed_index_sha256'] = previous.get('installed_index_sha256')
        state['placement_index_sha256'] = previous.get('placement_index_sha256',
                                                         previous.get('installed_index_sha256'))
        state['_prepared_from_collection_sha256'] = previous.get('_prepared_from_collection_sha256',
                                                                   previous.get('collection_sha256'))
        return state

    active=filesystem_path(app.folder/'spoken-player-names/paced-active'/identity)
    active_seals={}
    for folder, _, seal, files in combined.values():
        # Retain the package-relative path, so existing Ogg paths stay stable.
        relative=Path(*folder.parts[-5:])
        require(relative.parts[1:3]==('generated','sounds'),'Invalid admitted paced directory layout.')
        target=active/relative
        key=path_key(target)
        require(key not in active_seals, 'Two paced recipes use the same package path.')
        for name in files|{'dialogue.json'}:
            # Both cache locations update by replacement. An unverified reimport
            # cannot overwrite the inode held by the admitted collection.
            cache_file(target/name, folder/name, reuse_sealed_storage=True)
        active_seals[key]=seal
    root,seals=active,active_seals
    addons = None
    if data.get("addons_folder"):
        from lite_name_automatic import addons_folder
        addons = addons_folder(data["addons_folder"])
    output=filesystem_path(app.folder/'spoken-player-names'/ARCHIVE)
    with TemporaryDirectory(dir=output.parent,prefix='.paced-export-') as temporary:
        stage=filesystem_path(temporary)
        # Build privately. A rejected merge must not replace the previous download.
        package=stage/(identity+'.zip')
        result = package_names(root, package, trusted_deliveries=seals, decoded_clocks=decoded_clocks,
                               legacy_addons=addons, preserved_paced_names=preserved_names)
        require(not result['package_holds'] and result['packaged']==len(combined),
                'The paced package has ambiguous or held routes.')
        if not data.get('addons_folder'):
            from zipfile import ZipFile
            if output.is_file():
                with ZipFile(output) as existing:
                    has_names=any(not json.loads(existing.read(name)).get('paced_source')
                                  for name in existing.namelist() if name.endswith('/dialogue.json'))
                require(not has_names,'Choose the WoW AddOns folder to preserve existing personalized coverage.')
        if addons is not None:
            from lite_portable_package import BOOTSTRAP_INDEX, INDEX
            current=addons/INDEX
            expected_index=previous.get('installed_index_sha256')
            actual_index=file_hash(current) if current.is_file() else None
            if actual_index==digest(BOOTSTRAP_INDEX) and expected_index is None and not previous.get('seals'):
                # Setup reserves the add-on with an empty index before the listener
                # installs the first collection. Bind its bytes to the install CAS.
                expected_index=actual_index
            elif current.exists() and actual_index!=expected_index:
                from lite_mixed_update import rebuild_update
                # Preserve verified personalized routes in the installed update.
                package=stage/(identity+'-mixed.zip')
                result,expected_index=rebuild_update(app,addons,previous,
                    dict(root=path_key(root),seals=seals,preserved_name_seals=preserved_names),package)
            import_progress(progress, None, 'installing playback')
            result['placement_changed']=install_names(package,addons,check_index=True,
                                                      expected_index=expected_index)
            # This field certifies a typed-only index. A mixed index must always
            # pass reconstruction again, including on an idempotent reimport.
            installed_sha=file_hash(current) if package.name==identity+'.zip' else None
        else:installed_sha=previous.get('installed_index_sha256')
        state.update(root=path_key(root), seals=seals, installed_index_sha256=installed_sha)
        state['placement_index_sha256']=(file_hash(current) if data.get('addons_folder')
                                        else previous.get('placement_index_sha256',previous.get('installed_index_sha256')))
        if addons is not None and build_id:
            state["startup_verification"] = {
                "build": build_id,
                "addons": path_key(addons),
                "seals_sha256": digest(canonical(seals)),
                "preserved_name_seals_sha256": preserved_name_digest(state),
                "package_sha256": file_hash(package),
            }
        # Preserve the private mode of the former streamed publication.
        package.chmod(0o600)
        package.replace(output)
        app.store.set_setting(SETTING,state)
        return dict(result,download='/api/export/spoken-player-names',typed_paced=True,
                    native_word_timing_interpolated=True,native_wow_timing_verified=False)
