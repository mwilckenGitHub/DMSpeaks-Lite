import io, json, hashlib, tempfile, zipfile
from contextlib import nullcontext
from pathlib import Path
from model import decode_corpus
from audio import probe

MAX_EXPANDED = 512_000_000

def preflight_project(raw: bytes | zipfile.ZipFile, *, inspect_media=True) -> tuple:
    """Validate an archive, leaving a caller-owned ZIP open."""
    if isinstance(raw, zipfile.ZipFile):
        archive_context = nullcontext(raw)
    elif isinstance(raw, (bytes, bytearray)):
        try:
            archive_context = zipfile.ZipFile(io.BytesIO(raw), 'r')
        except zipfile.BadZipFile:
            raise ValueError("Invalid ZIP archive.") from None
    else:
        raise ValueError("Input must be bytes or an open project archive.")

    with archive_context as archive:
        infos = archive.infolist()
        if sum(i.file_size for i in infos) > MAX_EXPANDED:
            raise ValueError("Expanded project exceeds 512 MB.")

        names = [i.filename for i in infos]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate project archive entries.")

        namelist = set(names)

        if 'project.json' not in namelist:
            raise ValueError("Missing project.json.")
        if 'original.corpus' not in namelist:
            raise ValueError("Missing original.corpus.")

        try:
            manifest = json.loads(archive.read('project.json'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("Malformed project.json.")

        if not isinstance(manifest, dict):
            raise ValueError("Manifest must be a dictionary.")

        if manifest.get('format') != 'dialogue-studio-project' or type(manifest.get('version')) is not int or manifest['version'] not in (2, 3):
            raise ValueError("Unsupported project version.")
        if (manifest['version'] == 3) != ('corpus_entry_structure' in manifest):
            raise ValueError("Invalid project entry structure version.")

        from project_delimiter_library import validate as validate_project_delimiters
        manifest['project_delimiters'] = validate_project_delimiters(manifest.get('project_delimiters', []))

        name = manifest.get('name')
        if not isinstance(name, str) or not name:
            raise ValueError("Invalid project name.")

        original = archive.read('original.corpus')

        try:
            parsed = decode_corpus(original, name)
        except Exception:
            raise ValueError("Invalid corpus or name.")

        if 'fields' not in parsed or 'rows' not in parsed:
            raise ValueError("Corpus decode failed.")

        row_count = len(parsed['rows'])
        fields = parsed['fields']

        mapping = manifest.get('mapping')
        if not isinstance(mapping, dict):
            raise ValueError("Invalid mapping.")

        text_field = mapping.get('text')
        if text_field not in fields and text_field != '@segments':
            raise ValueError("Invalid source mapping.")

        if 'corpus_entry_structure' in manifest:
            from corpus_structure_snapshot import validate as validate_entry_structure
            manifest['corpus_entry_structure'] = validate_entry_structure(manifest['corpus_entry_structure'], original, mapping, row_count)

        overlay=manifest.get('corpus_overlay')
        if overlay is not None:
            if not isinstance(overlay,dict) or type(overlay.get('revision')) is not int or overlay['revision']<1 or not isinstance(overlay.get('mapping'),dict) or 'root' not in overlay:raise ValueError('Invalid working corpus snapshot.')
            from corpus_overlay import working_view
            parsed['mapping']=mapping
            working=working_view(parsed,overlay['root'],overlay['mapping'],overlay['revision'])
        else:working=parsed

        drafts = manifest.get('drafts', {})
        if not isinstance(drafts, dict):
            raise ValueError("Drafts must be a dictionary.")

        if not all(isinstance(k,str) and isinstance(v,str) for k,v in mapping.items()):raise ValueError('Invalid mapping value.')
        if not isinstance(manifest.get('settings',{}),dict):raise ValueError('Invalid project settings.')
        if type(manifest.get('source_text_locked',False)) is not bool:raise ValueError('Invalid source text protection setting.')
        source_config=manifest.get('corpus_audio_sources',{})
        if not isinstance(source_config,dict) or not isinstance(source_config.get('reference_ids',[]),list) or not isinstance(source_config.get('voice_bindings',{}),dict):raise ValueError('Invalid corpus audio source registry.')
        if any(not isinstance(k,str) for k in source_config.get('reference_ids',[])):raise ValueError('Invalid corpus audio source ID.')
        for choices in source_config.get('voice_bindings',{}).values():
            if not isinstance(choices,dict) or any(not isinstance(k,str) or not isinstance(v,dict) for k,v in choices.items()):raise ValueError('Invalid corpus voice bindings.')
        settings=manifest.get('settings',{})
        if settings.get('characters') is not None and (not isinstance(settings['characters'],list) or any(not isinstance(c,dict) or not isinstance(c.get('id'),str) or not isinstance(c.get('values'),dict) for c in settings['characters'])):raise ValueError('Invalid character profiles in project.')
        character_ids=[c['id'] for c in settings.get('characters') or []]
        if any(not ident for ident in character_ids) or len(set(character_ids))!=len(character_ids):raise ValueError('Character IDs must be unique non-empty strings.')
        if settings.get('layout') is not None and not isinstance(settings['layout'],dict):raise ValueError('Invalid saved layout.')
        if settings.get('theme') is not None and settings['theme'] not in ('light','dark','system'):raise ValueError('Invalid saved theme.')
        from workspace_state import validate_workspace
        entry_structure=manifest.get('corpus_entry_structure')
        workspace_count=entry_structure['state']['next_slot'] if entry_structure is not None else row_count
        workspace=settings.get('workspace')
        validate_workspace(workspace,workspace_count)
        if (entry_structure is not None and isinstance(workspace,dict) and 'index' in workspace
                and workspace['index'] not in entry_structure['state']['order']):
            raise ValueError('The workspace selects a hidden entry.')
        for key, value in drafts.items():
            if not isinstance(key, str) or not key.isascii() or not key.isdigit() or str(int(key)) != key:
                raise ValueError("Invalid draft key.")
            idx = int(key)
            if idx >= workspace_count:
                raise ValueError("Draft index out of bounds.")
            if not isinstance(value, str) or len(value) > 50000:
                raise ValueError("Invalid draft value.")

        assets = manifest.get('assets', [])
        references = manifest.get('references', [])
        takes = manifest.get('takes', [])

        if not isinstance(assets, list) or not isinstance(references, list) or not isinstance(takes, list):
            raise ValueError("Assets, references, and takes must be arrays.")

        asset_ids = set()
        for asset in assets:
            if not isinstance(asset, dict):
                raise ValueError("Asset must be a dictionary.")
            aid = asset.get('id')
            if not isinstance(aid, str) or not aid:
                raise ValueError("Asset ID must be a non-empty string.")
            if aid in asset_ids:
                raise ValueError("Duplicate asset ID.")
            asset_ids.add(aid)

            media_entry = asset.get('media_entry')
            if not isinstance(media_entry, str) or not media_entry.startswith('media/'):
                raise ValueError("Asset media_entry must start with media/.")
            if media_entry not in namelist:
                raise ValueError("Missing asset media entry.")

        if isinstance(workspace, dict) and workspace.get('entry_audio'):
            if workspace.get('dataset') != manifest['dataset']:
                raise ValueError('Remembered audio is outside this project corpus.')
            owners = {asset['id']: asset.get('record_id') for asset in assets}
            if any(owners.get(asset_id) != key for key, asset_id in workspace['entry_audio'].items()):
                raise ValueError('Remembered audio does not belong to its entry.')

        from data_audio_import import validate_inputs
        validate_inputs(archive,assets)
        reference_ids=set()
        for ref in references:
            if not isinstance(ref, dict):
                raise ValueError("Reference must be a dictionary.")
            ident=ref.get('id')
            if not isinstance(ident,str) or not ident or ident in reference_ids:raise ValueError('Reference IDs must be unique non-empty strings.')
            reference_ids.add(ident)
            media_entry = ref.get('media_entry')
            asset_id = ref.get('asset_id')

            if media_entry:
                if not isinstance(media_entry, str) or not media_entry.startswith('references/'):
                    raise ValueError("Reference media_entry must start with references/.")
                if media_entry not in namelist:
                    raise ValueError("Missing reference media entry.")
            elif asset_id:
                if not isinstance(asset_id,str) or asset_id not in asset_ids:
                    raise ValueError("Reference asset_id not found.")
            else:
                raise ValueError("Reference must have media_entry or asset_id.")

        from clip_groups import validate_config
        if 'replacement_groups' in manifest:
            group_count=manifest.get('corpus_entry_structure',{}).get('state',{}).get('next_slot',row_count)
            validate_config(manifest['replacement_groups'],group_count,asset_ids)
        from clip_group_schema import validate_group
        for asset in assets:
            descriptor=asset.get('replacement_groups')
            if descriptor is not None:
                if not isinstance(descriptor,dict) or not isinstance(descriptor.get('groups'),list):raise ValueError('Invalid arrangement replacement groups.')
                for raw_group in descriptor['groups']:
                    group=validate_group(raw_group)
                    if set(group['candidate_overrides'])-asset_ids:raise ValueError('An arrangement group references missing candidate audio.')

        from portable_references import validate_media
        corpus_refs=validate_media(manifest,working,namelist)

        for take in takes:
            if not isinstance(take, dict):
                raise ValueError("Take must be a dictionary.")
            take_asset_id = take.get('asset_id')
            if not isinstance(take_asset_id,str) or take_asset_id not in asset_ids:
                raise ValueError("Take asset_id not found.")

            piece_ids = take.get('piece_ids', [])
            if not isinstance(piece_ids, list):
                raise ValueError("Take piece_ids must be a list.")
            for pid in piece_ids:
                if not isinstance(pid,str) or pid not in asset_ids:
                    raise ValueError("Take piece_id not found.")

        from project_io import ASSET_REFERENCES, media_suffix, narration_references
        from replacement_playback_state import references as playback_references,validate as validate_playback
        from contextual_choices import references as contextual_references, validate_media as validate_contextual
        for asset in assets:
            for ident in playback_references(asset):
                if ident not in asset_ids:raise ValueError('Replacement preview dependency not found.')
            for ident in narration_references(asset):
                if ident not in asset_ids:raise ValueError('Narrator asset dependency not found.')
            for ident in contextual_references(asset):
                if ident not in asset_ids:raise ValueError('Saved contextual return dependency not found.')
            for key in ASSET_REFERENCES:
                value=asset.get(key)
                if value and (not isinstance(value,str) or value not in asset_ids):
                    raise ValueError('Asset dependency '+key+' not found.')
            layers=asset.get('layers',[])
            if not isinstance(layers,list):
                raise ValueError('Asset layers must be a list.')
            from guide_bindings import validate_graph
            validate_graph(layers)
            from replacement_snap import references as snap_references
            for layer in layers:
                if any(ident not in asset_ids for ident in snap_references(layer)):
                    raise ValueError('Replacement snap recording dependency not found.')
            slots = asset.get('slots', [])
            if not isinstance(slots, list):
                raise ValueError("Asset slots must be a list.")
            for slot in slots+layers:
                if not isinstance(slot, dict):
                    raise ValueError("Slot must be a dictionary.")
                slot_asset_id = slot.get('asset_id')
                if slot_asset_id and (not isinstance(slot_asset_id,str) or slot_asset_id not in asset_ids):
                    raise ValueError("Slot asset_id not found.")

        asset_map={asset['id']:asset for asset in assets}
        for asset in assets:
            validate_playback(asset,asset_map)
            validate_contextual(asset,asset_map)
        from processing_definition import validate as validate_processing_definition
        for asset in assets:
            if asset.get('kind') not in ('processing-audition','scoped-arrangement-preview'):
                continue
            parent=asset_map.get(asset.get('parent_assembly_id'))
            if asset.get('audition_only') is not True or not isinstance(parent,dict):
                raise ValueError('An effect audition needs its original arrangement and audition-only status.')
            try:
                graph = asset if asset['kind']=='scoped-arrangement-preview' else parent
                duration = asset.get('dry_sample_count') if graph is asset else parent.get('sample_count')
                validate_processing_definition(graph.get('tracks'),asset.get('processing_definition'),duration, layers=graph.get('layers'))
            except (ValueError, TypeError, KeyError) as error:
                raise ValueError('Invalid saved effect audition: '+str(error)) from error
        from background_duck import validate_ducking
        from background_loop import validate_loop
        for asset in assets:
            if 'track_hierarchy' in asset:
                from audio_track_hierarchy import TrackHierarchy
                stack = asset.get('processing_definition', asset.get('tracks'))
                hierarchy = TrackHierarchy.from_mapping(asset['track_hierarchy'], stack,
                                                        layers=asset.get('layers'), tracks=asset.get('tracks'))
                effective_locks = hierarchy.effective_locks()
                if any(track.get('group_locked', False) != effective_locks[track['id']]
                       for track in asset['tracks']):
                    raise ValueError('Track locks do not match their saved group hierarchy.')
            for track in asset.get('tracks',[]):
                if not isinstance(track,dict):raise ValueError('Invalid audio track.')
                if 'ducking' in track:
                    policy=validate_ducking(track['ducking'])
                    if policy['enabled'] and track.get('role') not in {'music','ambience'}:raise ValueError('Ducking requires a background track.')
            if any('group_locked' in track for track in asset.get('tracks', [])):
                from processing_group_lock import validate_lock_state
                validate_lock_state(asset['tracks'], asset.get('layers', []))
            for layer in asset.get('layers',[]):
                if layer.get('background_loop') is not None:
                    source=asset_map.get(layer.get('asset_id'),{})
                    count=source.get('sample_count')
                    if layer.get('clip_role') not in {'background_music','background_ambience'} or type(count) is not int or count<=0:raise ValueError('Invalid background loop source.')
                    validate_loop(layer['background_loop'],count)

        from native_join_evidence import portable_media
        native_returns = [context for asset in assets
                          if (context := portable_media(asset, namelist)) is not None]
        media_blobs = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            media = assets + [ref for ref in references if ref.get('media_entry')] + corpus_refs + native_returns
            for asset in media if inspect_media else ():
                entry = asset['media_entry']
                already_probed = entry in media_blobs
                data = media_blobs[entry] if already_probed else archive.read(entry)

                sha256_expected = asset.get('sha256')
                if sha256_expected:
                    if not isinstance(sha256_expected, str):
                        raise ValueError("Asset sha256 must be a string.")
                    actual_sha = hashlib.sha256(data).hexdigest()
                    if actual_sha != sha256_expected:
                        raise ValueError("Asset sha256 mismatch.")

                if already_probed:
                    continue
                probe_path = tmp_path / ('probe' + media_suffix(entry))
                probe_path.write_bytes(data)
                try:
                    probe(probe_path)
                except Exception:
                    raise ValueError("Invalid audio file.")
                finally:
                    probe_path.unlink(missing_ok=True)
                media_blobs[entry] = data

        dataset_hash = hashlib.sha256(original + json.dumps(mapping, sort_keys=True).encode()).hexdigest()[:20]
        if manifest.get('dataset') != dataset_hash:
            raise ValueError("Dataset hash mismatch.")

    return manifest, original, media_blobs
