"""Install the verified, shared speech samples used by the packaged name cutter."""
from hashlib import file_digest, sha256
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import NamedTemporaryFile


def install(package, archive, manifest_hash, archive_hash):
    if not all(re.fullmatch("[0-9a-f]{64}", value) for value in (manifest_hash, archive_hash)):
        raise ValueError("Invalid speech archive identity.")
    media = package / "media"
    target = media / f"paced-prefix-{manifest_hash}.zip"
    for path in (package, media, target):
        if path.is_symlink() or path.is_junction():
            raise ValueError("The installed speech data path is linked.")
    media.mkdir(exist_ok=True)
    if target.is_file():
        with target.open("rb") as existing:
            if file_digest(existing, "sha256").hexdigest() == archive_hash:
                return
    size = archive.stat().st_size
    if shutil.disk_usage(media).free < size:
        raise OSError(f"Installing speech data needs {size:,} free bytes on the Lite drive.")
    pending = None
    try:
        with archive.open("rb") as source, NamedTemporaryFile(dir=media, delete=False) as output:
            pending = Path(output.name)
            hashed = sha256()
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                output.write(chunk)
                hashed.update(chunk)
            if hashed.hexdigest() != archive_hash:
                raise ValueError("Speech archive verification failed. Run the complete installer again.")
            output.flush()
            os.fsync(output.fileno())
        pending.replace(target)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        install(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4])
    except (OSError, ValueError) as error:
        print(str(error), flush=True)
        raise SystemExit(1)
