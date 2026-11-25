"""Recheck current clip decisions before reusing a previously built export."""
import json, sqlite3
from pathlib import Path
from urllib.parse import quote
from content_review import assert_audio_usable


def read_arrangements(jobdir):
    root = Path(jobdir)/'library'
    manifest = root/'arrangements.json'
    if not manifest.is_file():
        return []  # Libraries created before edited arrangements were supported.
    return json.loads(manifest.read_text(encoding='utf-8'))


def assert_arrangement_delivery_usable(app, item):
    # A saved archive has already passed review. Check for later changes when
    # its arrangement is still available in the current working copy.
    try:
        asset = app.store.asset(item['arrangement_id'])
    except ValueError:
        return
    assert_audio_usable(asset, app.store)


def assert_library_delivery_usable(app, jobdir):
    database=Path(jobdir)/'index.sqlite'
    if not database.is_file():
        raise ValueError('That library export is not available.')
    with sqlite3.connect('file:'+quote(str(database.resolve()))+'?mode=ro',uri=True) as index:
        rows=index.execute("select c.metadata from clips c join used u on json_extract(c.metadata,'$.id')=u.clip")
        for raw, in rows:
            snapshot=json.loads(raw)
            # A prior ZIP must not bypass a new rejection/isolation requirement.
            # When the source asset was removed, its saved review still applies.
            with app.store.lock:
                row=app.store.db.execute('select metadata from assets where id=?',(snapshot['id'],)).fetchone()
            asset=json.loads(row[0]) if row else snapshot
            assert_audio_usable(asset,app.store)
    for item in read_arrangements(jobdir):
        assert_arrangement_delivery_usable(app, item)


def arrangement_preview_path(app, jobdir, arrangement_id, mix):
    from library_export import file_hash

    jobdir = Path(jobdir)
    if not (jobdir/'voxweave-audio-library.zip').is_file():
        raise ValueError('That library export is not available.')
    item = next((item for item in read_arrangements(jobdir)
                 if item['arrangement_id'] == arrangement_id), None)
    if item is None:
        raise ValueError('Unknown edited arrangement.')
    assert_arrangement_delivery_usable(app, item)
    if mix == 'authored':
        authored = item.get('authored_mix')
        if not authored:
            raise ValueError('This library has no complete edited Ogg.')
        relative, digest = authored['audio_path'], authored['audio_sha256']
    elif mix == 'positioned':
        relative, digest = item['preview_path'], None
    else:
        raise ValueError('Choose the edited Ogg or the positioned clip preview.')
    root = (jobdir/'library').resolve()
    file = (root/relative).resolve()
    if (not file.is_relative_to(root) or not file.is_file() or
            (digest and file_hash(file) != digest)):
        raise ValueError('The edited arrangement export is missing or changed. Build the library again.')
    return file
