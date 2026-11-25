"""Private file replacement and ordinary-error rollback; supervised local Qwen draft.

Multiple files are not a power-loss-atomic database transaction.
"""
from contextlib import contextmanager
from hashlib import sha256
import errno
import os
import tempfile
from pathlib import Path


def replace_private(path: Path, payload: bytes) -> None:
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    parent = path.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"Target parent directory does not exist: {parent}")
    fd, tmp_name = tempfile.mkstemp(dir=str(parent), prefix=".tmp_")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def commit_files(updates: list[tuple[Path, bytes]]) -> None:
    if not updates:
        return
    updates = [(Path(p).resolve(), payload) for p, payload in updates]
    paths = [p for p, _ in updates]
    if len(paths) != len(set(paths)):
        raise ValueError("Duplicate paths are not allowed")
    for _, payload in updates:
        if not isinstance(payload, bytes):
            raise TypeError("All payloads must be bytes")
    snapshots = [(path, path.read_bytes() if path.exists() else None) for path, _ in updates]
    published: list[tuple[Path, bytes | None]] = []
    try:
        for (path, payload), (_, original) in zip(updates, snapshots):
            replace_private(path, payload)
            published.append((path, original))
    except BaseException as exc:
        for path, original in reversed(published):
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    replace_private(path, original)
            except BaseException as rollback_exc:
                raise RuntimeError(
                    "Connection save failed and its rollback could not finish. "
                    "Restore the configuration backup before retrying."
                ) from exc
        raise

@contextmanager
def connection_writer(data: Path, filename='connections.lock'):
    """Serialize writes to the INI and its shared credential store."""
    from project_recovery import WorkingCopyLease

    data.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        lease = WorkingCopyLease(data, filename)
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EAGAIN):
            raise
        raise ValueError('Connection settings are being saved. Try again shortly.') from None
    try:
        yield
    finally:
        lease.close()


def connection_revision(path: Path, secrets: Path) -> str:
    """Bind a settings edit to both the reviewed INI and encrypted credentials."""
    digest = sha256()
    for file in (path, secrets):
        try:
            with file.open('rb') as stream:
                payload = stream.read(1_000_001)
        except FileNotFoundError:
            digest.update(b'missing')
            continue
        if len(payload) > 1_000_000:
            raise ValueError('Connection settings exceed 1 MB.')
        digest.update(b'file' + sha256(payload).digest())
    return digest.hexdigest()
