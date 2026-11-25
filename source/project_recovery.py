"""Discover retained project stores without trusting client-supplied paths.

Each managed folder owns its descriptor and OS lease. Descriptors are an index,
not a copy of SQLite: SQLite itself recovers its journal when the store reopens.
Never delete a working copy merely because its descriptor or database is damaged.
"""
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time


DESCRIPTOR = 'project-recovery.json'
IDENTIFIER = re.compile(r'project-[A-Za-z0-9_-]+\Z')


class WorkingCopyLease:
    """Hold one writer across processes; the OS releases this after a crash."""
    def __init__(self, folder, filename='project.lock'):
        self.file = (Path(folder) / filename).open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.file.seek(0, os.SEEK_END) == 0:
                    self.file.write(b'\0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.file.close()
            raise

    def close(self):
        if self.file.closed:
            return
        if os.name == 'nt':
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        self.file.close()


def read_descriptor(folder):
    path = folder / DESCRIPTOR
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError('Invalid project recovery record.')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] not in (1, 2, 3):
        raise ValueError('Unsupported project recovery record.')
    if not isinstance(data.get('dataset'), str) or not data['dataset']:
        raise ValueError('The recovery record has no corpus identity.')
    archive = data.get('archive')
    if data['version'] == 3:
        baseline = data.get('saved_content')
        if baseline is not None and (not isinstance(baseline, str) or not re.fullmatch(r'[0-9a-f]{64}', baseline)):
            raise ValueError('Invalid saved content fingerprint in recovery record.')
    else:
        # Older records cannot establish content equality with a saved file.
        data['saved_content'] = None
    # Version 2 also represents documents that have never been saved as files.
    if data['version'] in (2, 3) and 'archive' in data and 'saved_digest' in data and archive is None and data['saved_digest'] is None:
        return data
    if not isinstance(archive, str) or not Path(archive).is_absolute() or Path(archive).suffix.lower() != '.dmsproj':
        raise ValueError('Invalid saved project location in recovery record.')
    if not isinstance(data.get('saved_digest'), str) or not re.fullmatch(r'[0-9a-f]{64}', data['saved_digest']):
        raise ValueError('Invalid saved project fingerprint in recovery record.')
    return data


def write_descriptor(document):
    """Retain managed documents, including those without a saved archive."""
    if document.working_lease is None:
        return
    folder = document.app.folder
    data = {'version': 3, 'dataset': document.dataset, 'saved_content': document.saved_content,
            'archive': str(document.path) if document.path is not None else None, 'saved_digest': document.saved_digest,
            'recorded_at': time.time()}
    # The name is a display hint. It must never make a working copy unrecoverable.
    try:
        name = document.app.store.dataset_name(document.dataset)
        if isinstance(name, str) and name.strip():
            data['corpus_name'] = name.strip()[:256]
        display_name = document.app.store.setting('project_display_name')
        if isinstance(display_name, str) and display_name.strip():
            data['display_name'] = display_name.strip()[:256]
    except sqlite3.Error:
        pass
    staged = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder,
                                         prefix='.recovery-', delete=False) as output:
            staged = Path(output.name)
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        staged.replace(folder / DESCRIPTOR)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def recovery_folder(root, identifier):
    if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
        raise ValueError('Unknown recovery copy.')
    root = Path(root).resolve()
    folder = root / identifier
    if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != root:
        raise ValueError('Unknown recovery copy.')
    database = folder / 'studio.sqlite'
    if database.is_symlink() or not database.is_file():
        raise ValueError('The recovery database is missing.')
    return folder


def list_recovery(root):
    """Return closed copies and explicit unreadable entries; never modify data."""
    root = Path(root)
    if not root.exists():
        return []
    results = []
    for candidate in sorted(root.iterdir()):
        if not IDENTIFIER.fullmatch(candidate.name) or candidate.is_symlink() or not candidate.is_dir():
            continue
        lease = None
        try:
            folder = recovery_folder(root, candidate.name)
            try:
                lease = WorkingCopyLease(folder)
            except (BlockingIOError, PermissionError):
                continue  # In use, or cannot establish exclusive ownership.
            data = read_descriptor(folder)
            clocks = [folder / DESCRIPTOR, folder / 'studio.sqlite', folder / 'studio.sqlite-wal']
            updated = max(path.stat().st_mtime for path in clocks if path.exists())
            result = {'id': candidate.name, 'file_name': Path(data['archive']).name if data['archive'] else 'Untitled Project',
                      'path': data['archive'], 'updated': updated, 'available': True}
            display_name = data.get('display_name')
            if not data['archive'] and isinstance(display_name, str) and display_name.strip():
                result['file_name'] = display_name.strip()[:256]
            name = data.get('corpus_name')
            if isinstance(name, str) and name.strip():
                result['corpus_name'] = name.strip()[:256]
            results.append(result)
        except (OSError, ValueError, TypeError) as error:
            results.append({'id': candidate.name, 'file_name': candidate.name,
                            'available': False, 'error': str(error), 'updated': 0})
        finally:
            if lease is not None:
                lease.close()
    return sorted(results, key=lambda entry: entry['updated'], reverse=True)
