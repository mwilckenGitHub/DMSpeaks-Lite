"""Character-card portraits from Blizzard's public WoW Forever Armory."""

from concurrent.futures import Future
from contextlib import suppress
from hashlib import sha256
from html.parser import HTMLParser
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import BoundedSemaphore, Lock
from time import monotonic, time
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


# Blizzard's generic avatar IDs follow the playable-race IDs and model variants.
RACES = {"human": 1, "orc": 2, "dwarf": 3, "night elf": 4, "undead": 5,
         "tauren": 6, "gnome": 7, "troll": 8}
MODELS = {"male": 0, "female": 1}
UNKNOWN_PORTRAIT = "/blizzard-avatars/0-0.jpg"


def generic_portrait(race, gender):
    if race not in RACES or gender not in MODELS:
        return UNKNOWN_PORTRAIT
    return f"/blizzard-avatars/{RACES[race]}-{MODELS[gender]}.jpg"


REGIONS = {"us", "eu", "kr", "tw"}
DAY = 24 * 60 * 60
MAX_AVATAR = 256 * 1024


def installed_region(game_root):
    try:
        with (Path(game_root) / "WTF/Config.wtf").open(encoding="utf-8-sig", errors="replace") as config:
            for line in config:
                match = re.fullmatch(r'SET\s+portal\s+"(us|eu|kr|tw)"', line.strip(), re.I)
                if match:
                    return match[1].lower()
    except OSError:
        pass
    return None


def portrait_identity(values, region):
    """Forever identifies an Armory profile by region and both parts of its name."""
    if values.get("client") != "Forever" or region not in REGIONS:
        return None
    if isinstance(values.get("level"), int) and values["level"] < 10:
        return None
    name = values.get("name", "").strip()
    parts = name.split() if " " in name else [name, values.get("realm", "")]
    if len(parts) != 2 or not all(re.fullmatch(r"[^\W\d_]+(?:['-][^\W\d_]+)*", part) for part in parts):
        return None
    return (region, parts[0].lower(), parts[1].lower())


class _ArmoryPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_profile = False
        self.script = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_profile = dict(attrs).get("id") == "character-profile-mount-initial-state"

    def handle_data(self, text):
        if self.in_profile:
            self.script.append(text)

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_profile = False


def _read_asset(url, limit):
    request = Request(url, headers={"User-Agent": "DMSpeaks-Lite-WoW/1 (Armory portraits)"})
    with urlopen(request, timeout=5) as response:
        if urlsplit(response.geturl()).hostname != urlsplit(url).hostname:
            raise ValueError("Unexpected Armory host")
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Armory response exceeded its size limit")
        return data


def download_avatar(identity, *, ruleset_found=None):
    region, first, surname = identity
    slug = quote(first + "-" + surname, safe="-")
    url = f"https://worldofwarcraft.blizzard.com/en-us/forever/{region}/armory/character/{slug}"
    page = _ArmoryPage()
    page.feed(_read_asset(url, 2 * 1024 * 1024).decode("utf-8"))
    page.close()
    # The public page embeds its current avatar in JSON. Parse only that value.
    # A missing field or a changed page layout sends the card to its fallback.
    script = "".join(page.script).lstrip()
    assignment = re.match(r"var\s+characterProfileInitialState\s*=\s*", script)
    if not assignment:
        return None
    state, _ = json.JSONDecoder().raw_decode(script[assignment.end():])
    character = state["character"]
    actual = (character["region"], character["name"].lower(), character["surname"].lower())
    if actual != identity:
        return None
    ruleset = (character.get("ruleset") or {}).get("name")
    if ruleset_found and isinstance(ruleset, str) and ruleset.isprintable() and len(ruleset) <= 80:
        ruleset_found(ruleset)
    avatar = character["avatar"]["url"]
    parsed = urlsplit(avatar)
    if (parsed.scheme != "https" or parsed.netloc != "render.worldofwarcraft.com"
            or not re.fullmatch(rf"/forever-{region}/character/\d+/\d+-avatar(?:-sd)?\.jpg", parsed.path)):
        return None
    image = _read_asset(avatar, MAX_AVATAR)
    return image if image.startswith(b"\xff\xd8\xff") and image.endswith(b"\xff\xd9") else None


class ArmoryPortraits:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.lock = Lock()
        self.fetch_slots = BoundedSemaphore(3)
        self.pending = {}
        self.retry_after = {}

    def ruleset(self, identity):
        if identity is None:
            return None
        key = sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        try:
            return (self.folder / (key + ".ruleset")).read_text(encoding="utf-8")
        except OSError:
            return None

    def image(self, identity):
        if identity is None:
            return None
        key = sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        path = self.folder / (key + ".jpg")
        cached = None
        try:
            info = path.stat()
            if info.st_size <= MAX_AVATAR:
                image = path.read_bytes()
                if image.startswith(b"\xff\xd8\xff") and image.endswith(b"\xff\xd9"):
                    cached = image
                    if time() - info.st_mtime < DAY:
                        return cached
        except OSError:
            pass

        # Each profile has one lookup even with multiple open windows. Its
        # network wait never holds a profile-store lock or the audio queue.
        with self.lock:
            if self.retry_after.get(key, 0) > monotonic():
                return cached
            waiting = self.pending.get(key)
            if waiting is None:
                self.pending[key] = Future()
        if waiting is not None:
            return waiting.result()

        result = cached
        try:
            with self.fetch_slots:
                fresh = download_avatar(identity, ruleset_found=lambda value: self.save(path.with_suffix(".ruleset"), value.encode()))
            if fresh:
                result = fresh
                self.save(path, fresh)
            else:
                self.delay_retry(key)
        except (OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError):
            self.delay_retry(key)
        finally:
            with self.lock:
                self.pending.pop(key).set_result(result)
        return result

    def delay_retry(self, key):
        with self.lock:
            self.retry_after[key] = monotonic() + 15 * 60

    def save(self, path, image):
        self.folder.mkdir(parents=True, exist_ok=True)
        stream = NamedTemporaryFile(dir=self.folder, prefix=".portrait-", delete=False)
        temp = Path(stream.name)
        try:
            with stream:
                stream.write(image)
            os.replace(temp, path)
        finally:
            with suppress(OSError):
                temp.unlink()
