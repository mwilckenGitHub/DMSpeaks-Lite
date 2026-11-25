"""Report presence of the shipped WoW content without opening its audio files."""
from functools import lru_cache
import gzip
import json
from os import scandir
from os.path import normcase
from pathlib import Path
from time import time


INVENTORY = Path(__file__).with_name("formats") / "wow-installed-content.json.gz"


@lru_cache(maxsize=1)
def inventory():
    with gzip.open(INVENTORY, "rt", encoding="ascii") as stream:
        return json.load(stream)


def pack_present(addons, directories):
    try:
        for relative, files in directories.items():
            folder = addons / relative
            if folder.resolve() != folder:
                return False
            required = {normcase(name) for name in files}
            with scandir(folder) as entries:
                for entry in entries:
                    name = normcase(entry.name)
                    if name in required and entry.is_file(follow_symlinks=False):
                        required.remove(name)
                        if not required:
                            break
            if required:
                return False
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError):
        return None
    return True


def report(addons):
    """Serialize a fixed Lua data table. Unreadable collections remain unchecked."""
    lines = ["DMSpeaksInstalledContent = {", f"    checkedAt = {int(time())},"]
    for collection, directories in inventory().items():
        present = pack_present(addons, directories)
        if present is not None:
            value = "true" if present else "false"
            lines.append(f"    {collection} = {value},")
    return ("\n".join([*lines, "}", ""])).encode("ascii")
