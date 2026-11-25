"""Restricted, reload-based handoff between WoW SavedVariables and Lite."""
import json
from contextlib import contextmanager
from threading import Lock
from time import time
from pathlib import Path
import re
from urllib.parse import urlsplit
from zipfile import ZipFile

from lite_name_providers import ENGINES
from spoken_pack_lua import lua_string


ADDON = "DMSpeaksLite"
STATUS_ADDON = "DMSpeaksLiteStatus"
SETUP_LOCK = f".{ADDON}-setup.lock"
_STATUS_WRITER = Lock()
LEGACY_SNAPSHOT = "local _, addon = ...\naddon.snapshot"
RUNTIME_SNAPSHOT = "DMSpeaksLiteRuntimeSnapshot"
SETTINGS_FILES = (ADDON + ".toc", "Settings.lua", "Status.lua", "LocaleData.lua")
CLIENT_ICON = "Icons/dmspeaks-mark.tga"
MINIMAP_ICON = "Icons/minimap-waveform.tga"
PLAYER_NAMES_ICON = "Icons/player-names.tga"
STATUS_ICON = "Icons/fetch-status.tga"
CONTENT_ICONS = tuple("Icons/" + name + ".tga" for name in (
    "alliance-quests-framed", "horde-quests-framed", "shared", "gossip-framed", "dmspeaks-framed",
))
SOURCE = Path(__file__).with_name("lite_addon")
INSTALLER_URL_MARKER = "local liteInstallerURL = nil\n"
PROVIDERS = {"fish-s21-free", "fish-local", *("lite-" + x for x in ENGINES if x != "fish")}
FIELD = re.compile(r'\["([A-Za-z]+)"\]\s*=\s*([1-9][0-9]{0,2}|true|false|"(?:[^"\\\x00-\x1f]|\\["\\])*")\s*,?')
STAGES = {"accept":"Quest offer", "progress":"Quest progress", "complete":"Quest turn-in"}
BASIC = {"schema", "provider", "showProgress"}
IDENTITY = {"playerName", "playerRealm", "playerRace", "playerClass", "playerSex"}


def request_order(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}:[0-9]{1,12}", value):
        seconds, tick = value.split(":")
        return int(seconds), int(tick)
    return ()


def load_settings(path):
    path = Path(path).expanduser()
    if not path.is_absolute() or path.name.casefold() != (ADDON + ".lua").casefold() or (
            not path.is_file() or path.stat().st_size > 4096):
        raise ValueError("Choose the WoW SavedVariables DMSpeaksLite.lua file.")
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
                 if line.strip() and not line.lstrip().startswith("--")]
    except UnicodeError:
        raise ValueError("WoW settings need valid UTF-8.") from None
    if len(lines) < 5 or not re.fullmatch(r"DMSpeaksLiteSettings\s*=\s*\{", lines[0]) or lines[-1] != "}":
        raise ValueError("WoW has not written a supported Lite settings snapshot.")
    fields = {}
    for line in lines[1:-1]:
        match = FIELD.fullmatch(line)
        if not match or match[1] in fields:
            raise ValueError("The Lite settings contain unexpected data.")
        fields[match[1]] = json.loads(match[2])
    schema = fields.get("schema")
    faction = fields.pop("playerFaction", None)
    if faction is not None and (schema != 2 or not IDENTITY <= fields.keys()
                                or faction not in ("Alliance", "Horde", "Neutral")):
        raise ValueError("The saved WoW character faction is invalid.")
    race_id = fields.pop("playerRaceID", None)
    if race_id is not None and (schema != 2 or not IDENTITY <= fields.keys() or type(race_id) is not int):
        raise ValueError("The saved WoW race ID is invalid.")
    region = fields.pop("playerRegion", None)
    if region is not None and (schema != 2 or not IDENTITY <= fields.keys() or region not in ("us", "eu", "kr", "tw")):
        raise ValueError("The saved WoW region is invalid.")
    level = fields.pop("playerLevel", None)
    if level is not None and (schema != 2 or not IDENTITY <= fields.keys()
                              or type(level) is not int or level < 1):
        raise ValueError("The saved WoW character level is invalid.")
    valid_fields = (BASIC,) if schema == 1 else (BASIC | {"starterOnly"}, BASIC | {"starterOnly"} | IDENTITY)
    popup_seen = fields.pop("setupPromptSeen", None)
    angle = fields.pop("minimapAngle", None)
    if ((popup_seen is not None and (schema != 2 or type(popup_seen) is not bool))
            or (angle is not None and (schema != 2 or not isinstance(angle, str)
                                      or not re.fullmatch(r"[0-9]{1,3}", angle) or int(angle) >= 360))):
        raise ValueError("The saved WoW display settings are invalid.")
    progress_mode = fields.pop("progressMode", None)
    if progress_mode is not None and (schema != 2 or progress_mode not in ("bar", "compact", "off")):
        raise ValueError("The saved WoW progress display mode is invalid.")
    hud_corner = fields.pop("hudCorner", None)
    hud_alpha = fields.pop("hudAlpha", None)
    hud_minimized = fields.pop("hudMinimized", None)
    if ((hud_corner is not None and hud_corner not in ("BOTTOMRIGHT", "BOTTOMLEFT", "TOPLEFT"))
            or (hud_alpha is not None and (type(hud_alpha) is not int or not 25 <= hud_alpha <= 100))
            or (hud_minimized is not None and type(hud_minimized) is not bool)):
        raise ValueError("The saved WoW progress display settings are invalid.")
    fetch_request = fields.pop("fetchRequest", None)
    request_action = fields.pop("requestAction", None)
    selected_profile = fields.pop("selectedProfile", None)
    request_profile = fields.pop("requestProfile", None)
    for profile in (selected_profile, request_profile):
        if profile is not None and (schema != 2 or not isinstance(profile, str)
                                    or not re.fullmatch(r"[0-9a-f]{20}", profile)):
            raise ValueError("Choose a saved character in the WoW menu.")
    if request_profile is not None and fetch_request is None:
        raise ValueError("The character target needs a saved WoW request.")
    request_provider = fields.pop("requestProvider", None)
    provider_choice = fields.pop("providerChoice", None)
    last_request = fields.pop("lastRequest", None)
    fetch_target = fields.pop("fetchTarget", None)
    coverage = fields.pop("fetchCoverage", None)
    # UI-only cached coverage and a one-reload return marker are saved by the
    # add-on. They do not change the selected character or submit requests.
    cached = {key: fields.pop(key, None) for key in (
        "coverageName", "coverageRealm", "coverageScope", "coverageProvider",
        "coverageReady", "coverageTotal", "coverageTime")}
    if any(value is not None for value in cached.values()):
        if (not all(isinstance(value, str) and 0 < len(value) <= 100 for value in cached.values())
                or cached["coverageScope"] not in ("starting-area", "route", "all")
                or cached["coverageProvider"] not in PROVIDERS
                or any(not re.fullmatch(r"[0-9]{1,12}", cached[key]) for key in
                       ("coverageReady", "coverageTotal", "coverageTime"))
                or int(cached["coverageReady"]) > int(cached["coverageTotal"])
                or int(cached["coverageTotal"]) > 100000000):
            raise ValueError("The saved WoW coverage display is invalid.")
    returning = {key: fields.pop(key, None) for key in ("returnAction", "returnRequest", "returnName", "returnRealm")}
    if any(value is not None for value in returning.values()):
        if (returning["returnAction"] not in ("refresh", "check", "fetch", "pause")
                or (returning["returnAction"] != "refresh" and not request_order(returning["returnRequest"]))
                or (returning["returnAction"] == "refresh" and returning["returnRequest"] is not None)
                or any(not isinstance(returning[key], str) or not 0 < len(returning[key]) <= 100
                       for key in ("returnName", "returnRealm"))):
            raise ValueError("The saved WoW settings return marker is invalid.")
    if fetch_request is not None and (schema != 2 or not IDENTITY <= fields.keys()
            or not request_order(fetch_request)):
        raise ValueError("The saved WoW fetch request is invalid.")
    if ((last_request is not None and (schema != 2 or not request_order(last_request)))
            or (fetch_target is not None and
                (schema != 2 or not IDENTITY <= fields.keys() or not request_order(fetch_target)))):
        raise ValueError("The saved WoW request history is invalid.")
    if request_action is not None and (fetch_request is None or request_action not in ("fetch", "check", "pause", "configure")):
        raise ValueError("The saved WoW request action is invalid.")
    if provider_choice is not None and (schema != 2 or provider_choice not in PROVIDERS):
        raise ValueError("The WoW provider choice is invalid.")
    if request_provider is not None and (request_provider not in PROVIDERS
            or request_action not in ("fetch", "check", "configure")):
        raise ValueError("The provider must belong to a saved WoW fetch, check, or setup request.")
    provider = fields.get("provider")
    if coverage is not None and (schema != 2 or coverage not in ("starting-area", "route", "all")):
        raise ValueError("The saved WoW coverage selection is invalid.")
    if type(schema) is not int or schema not in {1, 2} or set(fields) not in valid_fields or (
            provider not in PROVIDERS or type(fields.get("showProgress")) is not bool):
        raise ValueError("The saved provider or progress setting is invalid.")
    preferences = {"provider_id":provider, "game_progress":fields["showProgress"]}
    if any(value is not None for value in (hud_corner, hud_alpha, hud_minimized)):
        preferences["hud"] = {"corner": hud_corner or "BOTTOMRIGHT",
                              "opacity": hud_alpha if hud_alpha is not None else 85,
                              "minimized": hud_minimized is True}
    if selected_profile:
        preferences["selected_profile"] = selected_profile
    if request_profile:
        preferences["request_profile"] = request_profile
    if progress_mode is not None:
        preferences["progress_mode"] = progress_mode
    if fetch_target is not None:
        preferences["fetch_target"] = fetch_target
    if fetch_request is not None:
        preferences["fetch_request"] = fetch_request
        preferences["request_action"] = request_action or "fetch"
        if request_provider is not None:
            preferences["request_provider"] = request_provider
        if request_action == "pause" and fetch_target is not None:
            preferences["pause_target"] = fetch_target
    if schema == 2:
        if type(fields["starterOnly"]) is not bool:
            raise ValueError("The saved starting-zone setting is invalid.")
        preferences["coverage"] = coverage or ("starting-area" if fields["starterOnly"] else "route")
    if IDENTITY <= fields.keys():
        name, realm, race, job, sex = (fields[key] for key in
            ("playerName", "playerRealm", "playerRace", "playerClass", "playerSex"))
        from lite_races import SKYBORNE_FACTIONS, normalize_race

        identity_race = normalize_race(race, race_id=race_id, faction=faction)
        if (not all(isinstance(value, str) for value in (name, realm, race, job, sex))
                or not re.fullmatch(r"[^\W\d_]+(?:[ '-][^\W\d_]+)*", name) or len(name) > 80
                or not realm or not realm.isprintable() or len(realm) > 100
                or (not re.fullmatch(r"[A-Za-z]{2,32}", race) and identity_race not in SKYBORNE_FACTIONS)
                or not re.fullmatch(r"[A-Za-z]{2,32}", job) or sex not in {"male", "female", ""}):
            raise ValueError("The saved WoW character is incomplete or invalid.")
        preferences["character"] = dict(name=name, realm=realm, client="Forever",
                                        race=identity_race if identity_race in SKYBORNE_FACTIONS else race,
                                        gender=sex, **{"class":job})
        if race_id is not None:
            preferences["character"]["race_id"] = race_id
        if faction is not None:
            preferences["character"]["faction"] = faction.casefold()
        if region is not None:
            # This metadata must not change the ID used by existing audio jobs.
            preferences["armory_region"] = region
        if level is not None:
            preferences["character_level"] = level
    return preferences


def imported_character(snapshot):
    from roster import import_roster

    values = snapshot.get("character")
    if values is not None:
        if "character_level" in snapshot:
            values = {**values, "level": snapshot["character_level"]}
        if "armory_region" in snapshot:
            values = {**values, "armory_region": snapshot["armory_region"]}
        return import_roster([values])[0]
    return None


def remember_character(store, character):
    roster = {row["id"]: row for row in store.setting("characters", [])}
    key = character["id"]
    previous = roster.get(key, {})
    current = {**character, "values": {**previous.get("values", {}), **character["values"]}}
    if "race_id" not in character["values"]:
        current["values"].pop("race_id", None)
    if current != previous:
        roster[key] = current
        store.set_setting("characters", list(roster.values()))


def import_settings(store, path):
    from lite_name_settings import DEFAULTS, PREFS

    snapshot = load_settings(path)
    picked = imported_character(snapshot)
    snapshot.pop("character", None)
    snapshot.pop("character_level", None)
    # WoW's legacy default is not an explicit provider choice.
    snapshot.pop("provider_id", None)
    snapshot.pop("fetch_request", None)
    snapshot.pop("request_action", None)
    snapshot.pop("request_profile", None)
    snapshot.pop("selected_profile", None)
    snapshot.pop("fetch_target", None)
    snapshot.pop("request_provider", None)
    snapshot.pop("pause_target", None)
    source = Path(path).expanduser()
    snapshot["settings_path"] = str(source)
    wtf = next((parent for parent in source.parents if parent.name.casefold() == "wtf"), None)
    if wtf is not None:
        addons = wtf.parent / "Interface" / "AddOns"
        if (addons / "Spoken/Spoken.toc").is_file() and (addons / "Spoken_Quests/Spoken_Quests.toc").is_file():
            snapshot["addons_folder"] = str(addons)
    if picked:
        remember_character(store, picked)
        snapshot["character_id"] = picked["id"]
    prefs = {**store.setting(PREFS, DEFAULTS), **snapshot}
    store.set_setting(PREFS, prefs)
    return prefs


def addon_metadata():
    """Read the interface and release version from the shipped settings manifest."""
    toc = (SOURCE / SETTINGS_FILES[0]).read_text(encoding="utf-8").splitlines()
    return tuple(next(line[len(prefix):] for line in toc if line.startswith(prefix))
                 for prefix in ("## Interface: ", "## Version: "))


def settings_members():
    files = (*SETTINGS_FILES, CLIENT_ICON, MINIMAP_ICON, *CONTENT_ICONS, PLAYER_NAMES_ICON, STATUS_ICON)
    return {**{name: SOURCE / name for name in files}, "LICENSE": SOURCE.parent / "LICENSE"}


def settings_with_installer_url(source, installer_url):
    parsed = urlsplit(installer_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or "\\" in installer_url or len(installer_url) > 512
            or any(char.isspace() or not char.isprintable() for char in installer_url)):
        raise ValueError("Choose a valid HTTPS download URL for the Lite installer.")
    if source.count(INSTALLER_URL_MARKER) != 1:
        raise ValueError("The add-on installer link could not be bound.")
    return source.replace(INSTALLER_URL_MARKER, f"local liteInstallerURL = {lua_string(installer_url)}\n", 1)


def build_archive(path, *, installer_url=None):
    settings = (settings_with_installer_url((SOURCE / "Settings.lua").read_text(encoding="utf-8"), installer_url)
                if installer_url is not None else None)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", strict_timestamps=False) as archive:
        for name, source in settings_members().items():
            if name == "Settings.lua" and settings is not None:
                archive.writestr(f"{ADDON}/{name}", settings)
            else:
                archive.write(source, f"{ADDON}/{name}")
    return path


def status_addon_files(addons):
    target = addons / STATUS_ADDON
    snapshot, toc = target / "Snapshot.lua", target / f"{STATUS_ADDON}.toc"
    if any(path.resolve() != path for path in (snapshot, toc)):
        raise ValueError("The native Lite status path leaves the selected AddOns folder.")
    return snapshot, toc


@contextmanager
def _status_lock(addons):
    """Serialize companion threads while retaining the installation's cross-process lease."""
    from lite_portable_io import hold

    with _STATUS_WRITER, hold(addons, SETUP_LOCK):
        yield


@contextmanager
def status_addon_update(addons, *, refresh_content=False):
    with _status_lock(addons):
        yield _ensure_status_addon(addons, refresh_content=refresh_content)


def ensure_status_addon(addons, *, refresh_content=False):
    with status_addon_update(addons, refresh_content=refresh_content):
        pass


def _ensure_status_addon(addons, *, refresh_content=False):
    from lite_portable_io import cache_bytes

    snapshot, toc = status_addon_files(addons)
    if not snapshot.is_file():
        cache_bytes(snapshot, (RUNTIME_SNAPSHOT + " = { schema = 1, state = \"idle\" }\n").encode())
    names = snapshot.with_name("Names.lua")
    icon = snapshot.parent / STATUS_ICON
    if names.resolve() != names:
        raise ValueError("The fetched Name index is outside the game add-ons folder.")
    if icon.resolve() != icon:
        raise ValueError("The Lite status icon is outside the game add-ons folder.")
    if not names.is_file():
        cache_bytes(names, b"DMSpeaksFetchedNames = {}\n")
    roster = snapshot.with_name("Profiles.lua")
    if roster.resolve() != roster:
        raise ValueError("The character list is outside the game add-ons folder.")
    if not roster.exists():
        cache_bytes(roster, b"DMSpeaksLiteRoster = {}\n")
    content = snapshot.with_name("Content.lua")
    if content.resolve() != content:
        raise ValueError("The installed content report is outside the game add-ons folder.")
    if refresh_content or not content.is_file():
        from lite_installed_content import report
        cache_bytes(content, report(addons))
    icon_bytes = (SOURCE / STATUS_ICON).read_bytes()
    if not icon.is_file() or icon.read_bytes() != icon_bytes:
        cache_bytes(icon, icon_bytes)
    interface, version = addon_metadata()
    contents = (f"## Interface: {interface}\n## Title: DMSpeaks Fetch Status\n## Version: {version}\n"
                "## Notes: Lets WoW read fetch progress and find the name clips Lite has saved. Keep enabled.\n"
                "## IconTexture: Interface\\AddOns\\DMSpeaksLiteStatus\\Icons\\fetch-status.tga\n"
                "Snapshot.lua\nNames.lua\nProfiles.lua\nContent.lua\n").encode()
    if not toc.is_file() or toc.read_bytes() != contents:
        cache_bytes(toc, contents)
    # Remove only the exact waveform written by earlier versions of this bridge.
    legacy_icon = snapshot.parent / CLIENT_ICON
    waveform = (SOURCE / CLIENT_ICON).read_bytes()
    if (legacy_icon.resolve() == legacy_icon and legacy_icon.is_file()
            and legacy_icon.stat().st_size == len(waveform) and legacy_icon.read_bytes() == waveform):
        legacy_icon.unlink(missing_ok=True)
    return snapshot


def install_settings_addon(addons):
    """Install the same settings files as the archive, with the TOC written last."""
    from lite_name_automatic import addons_folder
    from lite_portable_io import cache_bytes

    addons = addons_folder(str(addons.resolve()))
    target = addons / ADDON
    files = {target / name: source.read_bytes() for name, source in settings_members().items()}
    with _status_lock(addons):
        if any(path.resolve() != path for path in files):
            raise ValueError("The settings add-on path leaves the selected AddOns folder.")
        _ensure_status_addon(addons, refresh_content=True)
        settings_path = target / "Settings.lua"
        if settings_path.is_file() and INSTALLER_URL_MARKER.encode() in files[settings_path]:
            previous = re.search(r'^local liteInstallerURL = ("[^\r\n]*")$',
                                 settings_path.read_text(encoding="utf-8"), re.M)
            if previous:
                try:
                    address = json.loads(previous[1])
                    files[settings_path] = settings_with_installer_url(
                        files[settings_path].decode("utf-8"), address).encode("utf-8")
                except ValueError:
                    pass
        for path in sorted(files, key=lambda path: (path.suffix == ".toc", path)):
            if path.name == "Status.lua" and path.is_file():
                continue
            if not path.is_file() or path.read_bytes() != files[path]:
                cache_bytes(path, files[path])


def dialogue_stage(source_id):
    kind, *parts = source_id.split(":")
    if kind == "q":
        return STAGES.get(parts[-1] if parts else "", "Quest dialogue")
    return "Gossip" if kind == "g" else "Follow-up"


FALLBACK_RELOAD = "Reload WoW to apply fallback playback."
FINAL_PACING_PENDING = "Final playback pacing is still pending."


def fetch_summary(placed, statuses, placement_errors, *, paused, restart_required, fallback_reload=False):
    fallback = FALLBACK_RELOAD + " " if fallback_reload else ""
    if paused:
        return "paused", fallback + "Choose Resume Fetch in Lite to continue."
    if not statuses:
        return "unavailable", "The prepared catalog has no matching dialogue."

    reason = next((item["reason"] for item in (*placement_errors, *statuses) if item.get("reason")), None)
    if not placed:
        return "unavailable", fallback + (reason or "No audio was placed for this fetch.")
    next_step = ("Restart WoW to load the new audio add-on." if restart_required
                 else "Reload WoW to apply playback changes." if fallback_reload
                 else "Reload WoW to use your new audio.")
    pacing_pending = any(item.get("final_delivery_pending") for item in statuses)
    if placed == len(statuses) and pacing_pending:
        return "preview", f"{next_step} {FINAL_PACING_PENDING}"
    if placed == len(statuses):
        return "complete", next_step
    detail = f"{next_step} {reason or 'Some matching dialogues still need audio.'}"
    if pacing_pending:
        detail += " " + FINAL_PACING_PENDING
    return "unavailable", detail


def publish(addons, *, state, ready=0, total=0, npc=None, source_id=None, eta_seconds=None,
            request_id=None, detail=None, background=False, provider=None, coverage=None, character=None,
            settings_port=None, name_occurrence=None, name_total=None, dialogue_number=None,
            instance_progress=None, live_hud=False, excerpt=None, npc_number=None, npc_total=None):
    from lite_portable_io import cache_bytes

    if addons is None:
        return False
    root = addons.resolve()
    target = root / ADDON
    settings_toc = target / (ADDON + ".toc")
    if target.is_symlink() or not settings_toc.is_file():
        return False
    if settings_toc.is_symlink():
        raise ValueError("The native Lite status path leaves the selected AddOns folder.")
    if state not in {"connected", "planning", "fetching", "aligning", "coverage",
                     "complete", "preview", "paused", "stopped", "unavailable"}:
        raise ValueError("Invalid Lite progress state.")
    total = max(total, ready)
    fields = dict(schema=1, state=state, ready=ready, total=total,
                  generatedAt=int(time()))
    if request_id:
        fields["requestId"] = request_id
    if character and character.get("name") and character.get("realm"):
        fields["playerName"] = character["name"]
        fields["playerRealm"] = character["realm"]
    if detail:
        fields["detail"] = " ".join(detail.split())[:240].replace("|", "||")
    if excerpt:
        fields["dialogueExcerpt"] = " ".join(excerpt.split())[:100].replace("|", "||")
    if background:
        fields["background"] = 1
        if live_hud:
            fields["liveHud"] = 1
        if settings_port is not None:
            if type(settings_port) is not int or not 1 <= settings_port <= 65535:
                raise ValueError("The Fetch Settings port is invalid.")
            fields["settingsPort"] = settings_port
    if provider:
        fields["provider"] = "lite-fish_omni" if provider.startswith("lite-local-") else provider
    if coverage:
        fields["coverage"] = coverage
    if npc and source_id:
        fields["npc"] = npc
        fields["stage"] = dialogue_stage(source_id)
        if type(npc_number) is int and type(npc_total) is int and 1 <= npc_number <= npc_total:
            fields.update(npcNumber=npc_number, npcTotal=npc_total)
        if (type(dialogue_number) is int and 1 <= dialogue_number <= total
                and type(name_occurrence) is int and type(name_total) is int and 1 <= name_occurrence <= name_total
                and type(instance_progress) is int and 0 <= instance_progress <= 100):
            fields.update(dialogueNumber=dialogue_number, nameOccurrence=name_occurrence,
                          nameTotal=name_total, instanceProgress=instance_progress)
    if state == "complete" and ready == total:
        fields["etaSeconds"] = 0
    elif state in {"fetching", "aligning"} and eta_seconds is not None:
        fields["etaSeconds"] = eta_seconds
    with _status_lock(root):
        optional = next((line.partition(":")[2] for line in settings_toc.read_text(encoding="utf-8-sig").splitlines()
                         if line.startswith("## OptionalDeps:")), "")
        if STATUS_ADDON in {dependency.strip() for dependency in optional.split(",")}:
            status = _ensure_status_addon(root)
            binding = RUNTIME_SNAPSHOT
        else:
            status = target / "Status.lua"
            if status.resolve() != status:
                raise ValueError("The native Lite status path leaves the selected AddOns folder.")
            binding = LEGACY_SNAPSHOT
        content = binding + " = {\n" + "".join(
            f"    {key} = {lua_string(value) if isinstance(value, str) else value},\n"
            for key, value in fields.items()) + "}\n"
        cache_bytes(status, content.encode())
    return True
