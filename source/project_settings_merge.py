"""Preflight shared library settings before restoring a portable project.

Reference IDs may also be implicit corpus voice-profile IDs. A conflicting ID
cannot safely be renamed without interpreting arbitrary corpus fields, so refuse
that collision before any import writes. Character selections have explicit IDs.
"""
import copy
import hashlib
import json
from pathlib import Path

_REFERENCE_LOCAL_FIELDS = {'path', 'asset_id', 'media_entry'}


def _definition(reference):
    return {k: v for k, v in reference.items() if k not in _REFERENCE_LOCAL_FIELDS}


def plan_settings_merge(store, manifest, media):
    references = copy.deepcopy(store.setting('references', []) or [])
    existing = {ref['id']: ref for ref in references}
    assets = {asset['id']: asset for asset in manifest.get('assets', [])}
    add_reference_ids = set()
    for ref in manifest.get('references', []):
        ident = ref['id']
        if ident not in existing:
            add_reference_ids.add(ident)
            continue
        current = existing[ident]
        entry = ref.get('media_entry') or assets[ref['asset_id']]['media_entry']
        try:
            current_hash = hashlib.sha256(Path(current['path']).read_bytes()).digest()
        except (KeyError, OSError, TypeError) as exc:
            raise ValueError(f'Cannot verify existing voice reference "{ident}". Restore into a separate project folder or repair its local audio first.') from exc
        if _definition(ref) != _definition(current) or hashlib.sha256(media[entry]).digest() != current_hash:
            raise ValueError(f'Voice reference "{ident}" conflicts with an existing reference. Restore into a separate project folder to preserve both voice assignments.')

    characters = copy.deepcopy(store.setting('characters', []) or [])
    by_id = {item['id']: item for item in characters}
    character_ids = {}
    for source in manifest.get('settings', {}).get('characters') or []:
        item = copy.deepcopy(source)
        ident = item['id']
        if ident in by_id and by_id[ident] != item:
            # Stable content identity prevents repeated imports making duplicates.
            content = {k: v for k, v in item.items() if k != 'id'}
            suffix = hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            ident = item['id'] + '-import-' + suffix
            item['id'] = ident
            if ident in by_id and by_id[ident] != item:
                raise ValueError('Conflicting imported character identity.')
        character_ids[source['id']] = ident
        if ident not in by_id:
            characters.append(item)
            by_id[ident] = item
    return {'references': references, 'add_reference_ids': add_reference_ids,
            'characters': characters, 'character_ids': character_ids}
