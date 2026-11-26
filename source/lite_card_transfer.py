"""Portable character cards and their validated, already-fetched WoW recordings."""

from contextlib import nullcontext
from hashlib import sha256
import json
import re
from pathlib import Path
from sqlite3 import OperationalError
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Lock
from uuid import uuid4
from zipfile import BadZipFile, ZipFile, ZIP_DEFLATED, ZIP_STORED

from lite_share_playback import LIBRARY, SharePlayback, bind_folder, check_folder, mounted_network_folder, read_shared_file
from lite_storage_setup import location_label, provider_confirmed
from lite_name_bridge import ensure_status_addon
from lite_clip_bounds import saved_cuts
from lite_portable_io import cache_file, file_hash, hold
from lite_store_playback import (INDEX, JOURNAL, OVERLAY_LOCK, STATUS, lua_index, name_audio_relative,
                                 restore_loader, safe_path, saved_names, write_name_index)
from roster import import_roster


FORMAT = "dmspeaks-wow-name-cards/1"
MANIFEST = "cards.json"
MAX_INDEX = 32 * 1024**2
MAX_CLIP = 32 * 1024**2
MAX_BACKUP = 2 * 1024**3
CARD_FIELDS = {"name", "realm", "armory_region", "client", "region", "race", "class", "gender", "level", "faction"}
CLOUD = {"googledrive", "onedrive", "icloud", "dropbox"}
MIRRORS = {"syncthing", "rsync", "rclone"}
MOUNTS = {"tailscale", "sftp", "nfs", "webdav"}
MEDIA = CLOUD | MIRRORS | MOUNTS | {"shared", "nas", "network"}


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def media(index):
    """A name occurrence can own a second Ogg containing its fixed prefix."""
    for choices in index.values():
        for item in choices.values():
            yield item
            if item.get("prefix"):
                yield item["prefix"]


def card_fingerprints(cards, index):
    """Two realms can share a spoken name while keeping distinct character cards."""
    per_name = {}
    for slot, choices in sorted(index.items()):
        for name, clip in choices.items():
            row = [slot, clip["sha256"], (clip.get("prefix") or {}).get("sha256")]
            if cuts := saved_cuts(clip):
                row.append(cuts)
            per_name.setdefault(name, []).append(row)
    prints = {"audio:" + name: sha256(json_bytes(rows)).hexdigest() for name, rows in per_name.items()}
    for card in import_roster(cards):
        values = card["values"]
        spoken = values["name"].split(" ", 1)[0].casefold()
        prints[card["id"]] = sha256(json_bytes([values, per_name.get(spoken, [])])).hexdigest()
    return prints


def validate_manifest(raw):
    if len(raw) > MAX_INDEX:
        raise ValueError("This name-card index is too large.")
    try:
        info = json.loads(raw)
        if info["format"] != FORMAT:
            raise ValueError("Choose a DMSpeaks WoW name-card backup.")
        cards, index = info["cards"], info["names"]
        if not isinstance(cards, list) or len(cards) > 2000 or any(not isinstance(c, dict) or c.keys() - CARD_FIELDS for c in cards):
            raise ValueError("The backup contains an invalid character card.")
        for card in cards:
            if any(not isinstance(v, (str, int)) or isinstance(v, str) and len(v) > 256 for v in card.values()):
                raise ValueError("The backup contains invalid character fields.")
        for card in import_roster(cards):
            if card["values"].get("client") != "Forever" or not card["values"].get("realm"):
                raise ValueError("A WoW character card is incomplete.")
        lua_index(index)
        for clip in media(index):
            name_audio_relative(clip)
        return cards, index
    except (KeyError, TypeError, AttributeError, UnicodeError, RecursionError) as exc:
        raise ValueError("This backup has an invalid character or audio index.") from exc


def checked_audio(data, expected):
    header = 27 + data[26] if len(data) >= 27 else 0
    if (len(data) > MAX_CLIP or not data.startswith(b"OggS\x00") or not header
            or data[header:header + 7] != b"\x01vorbis" or sha256(data).hexdigest() != expected):
        raise ValueError("A recording is incomplete or changed. Try the original backup again.")
    return data


def write_shared_file(path, data):
    # The mount can disappear after a check. Never recreate missing parent directories.
    if path.parent.resolve() != path.parent or path.is_symlink():
        raise ValueError("The selected library contains a redirected folder or file.")
    with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(data)
        except BaseException:
            stream.close()
            staged.unlink(missing_ok=True)
            raise
    try:
        staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


class CardTransfer:
    def __init__(self, ingress):
        self.ingress = ingress
        self.addons = ingress.addons
        self.store = ingress.app.store
        self.scope = "lite_wow_profiles_" + str(ingress.root)
        self.sync_key = "lite_name_card_sync_" + str(ingress.root)
        self.lock = Lock()
        self.playback = SharePlayback(self.addons)
        self.playback_config_lock = Lock()
        self.share_offline = False
        self.last_scan = None
        self.fingerprint_cache = (None, {})
        self.object_stamps = {}
        self.status = {"state": "idle"}

    def snapshot(self, selected=None):
        with self.ingress.profile_lock:
            known = self.ingress.known_players(self.ingress.popup_player(), self.ingress.name_recording_counts())
            if selected is not None:
                if selected not in known:
                    raise ValueError("Choose a saved character card to export.")
                known = {selected: known[selected]}
            cards = [{k: v for k, v in row.items() if k in CARD_FIELDS} for _, row in sorted(known.items())]
        restore_loader(self.addons)
        with hold(self.addons, OVERLAY_LOCK):
            index = saved_names(self.addons)
            if index and safe_path(self.addons, STATUS / "Names.lua").read_bytes() != lua_index(index):
                raise ValueError("Lite is updating saved name audio. Try the export again.")
        if selected is not None:
            # WoW shares name audio across profiles with the same spoken first name.
            spoken = next(iter(known.values()))["name"].split(" ", 1)[0].casefold()
            index = {slot: {spoken: choices[spoken]} for slot, choices in index.items() if spoken in choices}
        manifest = json_bytes({"format": FORMAT, "cards": cards, "names": index})
        validate_manifest(manifest)
        sources = {clip["sha256"]: safe_path(self.addons, name_audio_relative(clip)) for clip in media(index)}
        return manifest, sources

    def export_zip(self, stream, selected=None):
        raw, sources = self.snapshot(selected)
        if len(raw) + sum(path.stat().st_size + 240 for path in sources.values()) > MAX_BACKUP - 65536:
            raise ValueError("Export individual character cards to keep each backup under 2 GiB.")
        with ZipFile(stream, "w", allowZip64=True) as archive:
            archive.writestr(MANIFEST, raw, compress_type=ZIP_DEFLATED)
            for checksum, path in sorted(sources.items()):
                archive.writestr("audio/" + checksum + ".ogg", checked_audio(path.read_bytes(), checksum),
                                 compress_type=ZIP_STORED)

    def save_backup(self, folder, selected=None):
        if not isinstance(folder, str) or not Path(folder).is_absolute() or not Path(folder).is_dir():
            raise ValueError("Choose an existing destination folder for the backup.")
        if selected is not None and (not isinstance(selected, str) or not selected):
            raise ValueError("Choose a saved character card to export.")
        folder = Path(folder).resolve()
        backup = folder / f"DMSpeaks-name-cards-{uuid4().hex[:12]}.zip"
        staging = None
        try:
            with NamedTemporaryFile(dir=folder, prefix=".dmspeaks-backup-", suffix=".tmp", delete=False) as stream:
                staging = Path(stream.name)
                self.export_zip(stream, selected)
            staging.rename(backup)
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)
        return backup

    def import_zip(self, stream):
        try:
            with ZipFile(stream) as archive:
                manifest = archive.getinfo(MANIFEST)
                if manifest.file_size > MAX_INDEX:
                    raise ValueError("The name-card index is too large.")
                raw = archive.read(manifest)
                _, index = validate_manifest(raw)
                expected = {MANIFEST} | {"audio/" + clip["sha256"] + ".ogg" for clip in media(index)}
                members = archive.infolist()
                if (len(members) != len(expected) or set(archive.namelist()) != expected
                        or any(item.file_size > MAX_CLIP for item in members)
                        or sum(item.file_size for item in members) > MAX_BACKUP):
                    raise ValueError("The backup's recordings do not match its index.")
                return self.merge(raw, lambda digest: archive.read("audio/" + digest + ".ogg"))
        except (BadZipFile, KeyError, RuntimeError) as exc:
            raise ValueError("Choose a complete DMSpeaks name-card ZIP file.") from exc

    def merge(self, raw, load_audio, *, request_locked=False):
        cards, incoming = validate_manifest(raw)
        assets = {clip["sha256"] for clip in media(incoming)}
        # Validate every object before activating any of its dialogue choices.
        with TemporaryDirectory(prefix="dmspeaks-card-import-") as temp:
            staging = Path(temp)
            total = len(raw)
            for digest in assets:
                data = checked_audio(load_audio(digest), digest)
                total += len(data)
                if total > MAX_BACKUP:
                    raise ValueError("This collection exceeds the name card import limit.")
                (staging / digest).write_bytes(data)
            restore_loader(self.addons)
            with nullcontext() if request_locked else self.ingress.request_lock:
                if self.ingress.active:
                    raise ValueError("A fetch or coverage check is active. Restore or sync after it finishes.")
                with hold(self.addons, OVERLAY_LOCK):
                    added, kept, conflicts = self.merge_audio(incoming, staging)
        with self.ingress.profile_lock:
            known = self.store.setting(self.scope, {})
            for card in import_roster(cards):
                existing = known.get(card["id"], {})
                known[card["id"]] = {**card["values"], **existing}
            self.store.set_setting(self.scope, known)
            self.ingress.roster_revision = None
        return {"cards": len(cards), "added": added, "kept": kept, "conflicts": conflicts}

    def merge_audio(self, incoming, staging):
        current = saved_names(self.addons)
        added = kept = conflicts = 0
        metadata_changed = False
        for slot, choices in incoming.items():
            target = current.setdefault(slot, {})
            for name, choice in choices.items():
                old = target.get(name)
                if old is not None:
                    # Prefer a playable recording already owned by this computer.
                    old_parts = [old, old["prefix"]] if old.get("prefix") else [old]
                    if all((path := safe_path(self.addons, name_audio_relative(part))).is_file()
                           and file_hash(path) == part["sha256"] for part in old_parts):
                        kept += 1
                        if (old["sha256"], old.get("prefix")) != (choice["sha256"], choice.get("prefix")):
                            conflicts += 1
                        elif old["samples"] != choice["samples"]:
                            conflicts += 1
                        elif cuts := saved_cuts(choice):
                            if any(key in old and old[key] != cut for key, cut in cuts.items()):
                                conflicts += 1
                            elif any(key not in old for key in cuts):
                                try:
                                    saved_cuts({**old, **cuts})
                                except ValueError:
                                    conflicts += 1
                                else:
                                    old.update(cuts)
                                    metadata_changed = True
                        continue
                for part in [choice, choice["prefix"]] if choice.get("prefix") else [choice]:
                    dest = safe_path(self.addons, name_audio_relative(part))
                    if dest.exists() and file_hash(dest) != part["sha256"]:
                        # Two source machines can use the same human-readable filename.
                        # Use the loader's content-addressed path to keep both recordings.
                        part.pop("file", None)
                        dest = safe_path(self.addons, name_audio_relative(part))
                    if not dest.is_file() or file_hash(dest) != part["sha256"]:
                        cache_file(dest, staging / part["sha256"])
                target[name] = choice
                added += 1
        if added or metadata_changed:
            ensure_status_addon(self.addons)
            write_name_index(self.addons, current)
        return added, kept, conflicts

    def sync_state(self):
        config = self.store.setting(self.sync_key, {})
        return {"folder": config.get("folder", ""), "medium": config.get("medium", "shared"),
                "lan_only": config.get("lan_only", False), "nas": config.get("nas", False),
                "via_tailscale": config.get("via_tailscale", False), "cache_offline": config.get("cache_offline", False), **self.status}

    def storage_states(self, known):
        config = self.store.setting(self.sync_key, {})
        if not config.get("folder"):
            return {}
        cards = [{k: v for k, v in row.items() if k in CARD_FIELDS} for _, row in sorted(known.items())]
        signature = (json_bytes(cards), self.ingress.recording_revision)
        if signature != self.fingerprint_cache[0]:
            self.fingerprint_cache = (signature, card_fingerprints(cards, saved_names(self.addons)))
        receipt = self.store.setting(self.sync_key + "_receipt", {})
        verified = receipt.get("cards", {}) if receipt.get("folder") == config["folder"] else {}
        result = {}
        for ident, fingerprint in self.fingerprint_cache[1].items():
            if self.status.get("error") or self.share_offline:
                state = "previous" if verified.get(ident) == fingerprint else "pending"
            elif config.get("lan_only"):
                state = "local"
            elif verified.get(ident) == fingerprint:
                state = "synced"
            else:
                state = "pending"
            result[ident] = {"state": state, "medium": config.get("medium", "shared"), "path": config["folder"]}
            spoken = known.get(ident, {}).get("name", "").split(" ", 1)[0].casefold()
            result[ident]["cached"] = bool(config.get("cache_offline") and "audio:" + spoken in self.fingerprint_cache[1])
            result[ident]["nas"] = config.get("nas", False)
            result[ident]["via_tailscale"] = config.get("via_tailscale", False)
            if config.get("medium") not in CLOUD:
                result[ident]["location"] = location_label(config["folder"])
        return result

    def configure_sync(self, value, medium="shared", lan_only=None, nas=False, via_tailscale=False, cache_offline=False):
        if not isinstance(value, str) or not isinstance(medium, str) or medium not in MEDIA:
            raise ValueError("Choose a storage service and an existing folder.")
        if lan_only is not None and not isinstance(lan_only, bool):
            raise ValueError("Choose whether this location is limited to your LAN.")
        if any(type(flag) is not bool for flag in (nas, via_tailscale, cache_offline)):
            raise ValueError("Choose a valid storage connection.")
        with self.lock, self.ingress.request_lock:
            if self.ingress.active or self.store.setting(self.sync_key + "_fetch", {}):
                raise ValueError("Pause the name fetch and let Lite finish syncing before changing storage.")
            old = self.store.setting(self.sync_key, {})
            config = {"device": old.get("device") or uuid4().hex}
            if value.strip():
                path = Path(value.strip().strip('"')).expanduser()
                if not path.is_absolute() or not path.is_dir():
                    raise ValueError("Choose an existing folder. Connect the drive first if it is offline.")
                path = path.resolve()
                if medium in MOUNTS and not mounted_network_folder(path):
                    raise ValueError("Choose the mounted network folder, or connect it in your file manager first.")
                if medium == "shared" and mounted_network_folder(path):
                    medium = "network"
                routed = medium == "tailscale" or (medium in MOUNTS | {"nas", "network"} and via_tailscale)
                same = str(path) == old.get("folder") and old.get("binding")
                config.update(folder=str(path), medium=medium, binding=old["binding"] if same else bind_folder(path),
                              cache_offline=cache_offline and medium in CLOUD | MIRRORS,
                              nas=nas and medium in MOUNTS | {"network"}, via_tailscale=routed,
                              lan_only=medium not in CLOUD | MIRRORS and not routed and lan_only is not False)
            if old != config:
                self.store.set_setting(self.sync_key + "_receipt", {})
            with self.playback_config_lock:
                self.playback.publish(bool(config.get("folder")) and not config.get("cache_offline"), False)
                self.store.set_setting(self.sync_key, config)
                self.share_offline = bool(config.get("folder"))
            self.last_scan = None
            self.status = {"state": "waiting" if config.get("folder") else "off"}
        return self.sync_state()

    def check_playback(self, config, *, fetch_job=None):
        try:
            if config.get("folder"):
                check_folder(config)
        except (ValueError, OSError):
            # A slow check may return after a different fetch or storage configuration took over.
            if fetch_job is not None:
                with self.ingress.request_lock:
                    active = self.ingress.active
                    if active and active[0] == fetch_job and self.store.setting(self.sync_key, {}) == config:
                        self.ingress.pause_fetch(fetch_job, reason="The shared name library is unavailable. "
                                                 "Reconnect it, then choose Resume Fetch.")
            self.publish_playback(config, False)
            raise
        self.publish_playback(config, True)

    def publish_playback(self, config, reachable):
        with self.playback_config_lock:
            if self.store.setting(self.sync_key, {}) == config:
                self.playback.publish(bool(config.get("folder")) and not config.get("cache_offline"), reachable)
                self.share_offline = bool(config.get("folder")) and not reachable

    def watch_playback(self, stop):
        while not stop.is_set():
            try:
                config = self.store.setting(self.sync_key, {})
                active = self.ingress.active
                fetch_job = active[0] if active and active[2] == "fetch" else None
                self.check_playback(config, fetch_job=fetch_job)
            except (ValueError, OSError, OperationalError):
                # Retry the game's playback indicator next poll. Its local files can
                # be temporarily locked independently of the audio library itself.
                pass
            stop.wait(5)

    def reserve_fetch(self, owner):
        """Reserve a spoken first name before submitting work on a directly shared filesystem."""
        config = self.store.setting(self.sync_key, {})
        if not config.get("folder"):
            return
        if config["medium"] in CLOUD | MIRRORS:
            self.restore_before_fetch()
            return  # Independent mirrors cannot provide cross-computer mutual exclusion.
        spoken = owner["name"].split()[0].casefold()
        key = sha256(spoken.encode()).hexdigest()
        library = Path(config["folder"]) / LIBRARY
        check_folder(config)
        claim = library / ("fetch-" + key)
        own = config["device"]
        remembered = self.store.setting(self.sync_key + "_fetch", {})
        reservation = {"key": key, "device": own}
        if remembered and remembered != reservation:
            raise ValueError("The previous fetch is still syncing on this computer.")
        def owned_here():
            return (claim.resolve() == claim and (claim / own).is_file()
                    and read_shared_file(claim / own, 6) == b"active")

        if claim.exists() and not owned_here():
            raise ValueError("Another computer is fetching this name. Pause it there and let it finish syncing.")
        # Rename a complete, nonempty directory. Another device's reservation
        # cannot be replaced, including if either Lite process stops unexpectedly.
        stage = library / (".fetch-" + uuid4().hex)
        stage.mkdir()
        marker = stage / own
        try:
            marker.write_bytes(b"active")
            # Save the name first so a restart or database lock cannot orphan our claim.
            if not remembered:
                self.store.set_setting(self.sync_key + "_fetch", reservation)
            if not claim.exists():
                try:
                    stage.rename(claim)
                except OSError:
                    if not claim.exists():
                        raise
            if not owned_here():
                raise ValueError("Another computer is fetching this name. Pause it there and let it finish syncing.")
        finally:
            if stage.exists():
                marker.unlink(missing_ok=True)
                stage.rmdir()
        self.restore_before_fetch()

    def release_saved_fetch(self):
        saved = self.store.setting(self.sync_key + "_fetch", {})
        if not saved or self.ingress.active or self.store.setting("lite_game_fetch_inflight", {}):
            return
        config = self.store.setting(self.sync_key, {})
        if (not re.fullmatch(r"[a-f0-9]{64}", saved["key"])
                or saved["device"] != config.get("device")):
            raise ValueError("The saved fetch reservation does not match this installation.")
        claim = Path(config["folder"]) / LIBRARY / ("fetch-" + saved["key"])
        if claim.exists() and (claim / saved["device"]).is_file():
            read_shared_file(claim / saved["device"], 6)
            retiring = claim.with_name(".finished-" + uuid4().hex)
            claim.rename(retiring)
            (retiring / saved["device"]).unlink()
            retiring.rmdir()
        self.store.set_setting(self.sync_key + "_fetch", {})

    def finish_fetch_sync(self):
        # The game watcher holds request_lock here. A background sync may
        # already own the transfer lock. It will finish this handoff instead.
        if not self.lock.acquire(blocking=False):
            return
        try:
            config = self.store.setting(self.sync_key, {})
            if not config.get("folder"):
                return
            self.sync_folder(Path(config["folder"]), config["device"], config["medium"], request_locked=True)
            self.release_saved_fetch()
        except (OSError, ValueError, OperationalError) as exc:
            self.status = {"state": "waiting", "error": str(exc)}
        finally:
            self.lock.release()

    def watch(self, stop):
        # Sync is optional and independent of both the WoW watcher and speech worker.
        while not stop.is_set():
            try:
                if self.store.setting(self.sync_key, {}).get("folder"):
                    self.synchronize()
            except (OSError, ValueError, OperationalError) as error:
                self.status = {"state": "waiting", "error": str(error)}
            if stop.wait(60):
                break

    def synchronize(self):
        with self.lock:
            config = self.store.setting(self.sync_key, {})
            if not config.get("folder"):
                return self.sync_state()
            self.status = {"state": "syncing"}
            try:
                self.sync_folder(Path(config["folder"]), config["device"], config.get("medium", "shared"))
                with self.ingress.request_lock:
                    self.release_saved_fetch()
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self.status = {"state": "waiting", "error": str(exc)}
            return self.sync_state()

    def restore_before_fetch(self):
        """Called under request_lock. Never wait on a background sync that needs that lock."""
        config = self.store.setting(self.sync_key, {})
        if not config.get("folder"):
            return
        if not self.lock.acquire(blocking=False):
            raise ValueError("The shared library is updating. Try Fetch Names when the update finishes.")
        try:
            self.sync_folder(Path(config["folder"]), config["device"], config.get("medium", "shared"), request_locked=True)
        except (OSError, ValueError) as error:
            self.status = {"state": "waiting", "error": str(error)}
            raise ValueError("Could not check the shared name library. Reconnect it or disconnect it in Setup to fetch locally.") from error
        finally:
            self.lock.release()

    def sync_folder(self, folder, device, medium, *, request_locked=False):
        config = self.store.setting(self.sync_key, {})
        if not config.get("binding"):
            # Older installs have an existing manifest on the intended share.
            if (folder / LIBRARY / "libraries" / (device + ".json")).is_file():
                config["binding"] = bind_folder(folder)
                self.store.set_setting(self.sync_key, config)
        self.check_playback(config)
        shared = folder / LIBRARY
        libraries, objects = shared / "libraries", shared / "audio"
        if any(p.resolve() != p for p in (shared, libraries, objects)):
            raise ValueError("Choose a name card library with ordinary folders.")
        libraries.mkdir(exist_ok=True)
        objects.mkdir(exist_ok=True)
        local, sources = self.snapshot()
        own = libraries / (device + ".json")
        peers = []
        for path in libraries.glob("*.json"):
            if path != own and re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                peers.append(path)
                if len(peers) > 256:
                    raise ValueError("The library contains too many device indexes.")
        peers.sort()
        revisions = [(p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in peers]

        added = conflicts = 0
        peer_cards = {}
        rescan = self.last_scan != (sha256(local).digest(), revisions)
        for peer in peers:
            if peer.stat().st_size > MAX_INDEX:
                raise ValueError("A synced card index is too large.")
            def read_shared(digest):
                source = objects / (digest + ".ogg")
                try:
                    data = read_shared_file(source, MAX_CLIP)
                except FileNotFoundError:
                    data = None
                # Peers may already advertise a missing or damaged copy of a local take.
                local_copy = sources.get(digest)
                if local_copy is not None and (data is None or sha256(data).hexdigest() != digest):
                    try:
                        verified = checked_audio(read_shared_file(local_copy, MAX_CLIP), digest)
                    except (OSError, ValueError):
                        pass
                    else:
                        write_shared_file(source, verified)
                        return read_shared_file(source, MAX_CLIP)
                if data is None:
                    raise ValueError("Waiting for recordings to arrive in the sync folder.")
                return data
            raw = read_shared_file(peer, MAX_INDEX)
            peer_cards.update(card_fingerprints(*validate_manifest(raw)))
            result = self.merge(raw, read_shared, request_locked=request_locked) if rescan and not self.ingress.active else {"added": 0, "conflicts": 0}
            added += result["added"]
            conflicts += result["conflicts"]
        if peers:
            local, sources = self.snapshot()
        for checksum, source in sources.items():
            dest = objects / (checksum + ".ogg")
            if dest.is_symlink():
                raise ValueError("The sync folder contains a linked audio file.")
            stat = dest.stat() if dest.is_file() else None
            stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino) if stat else None
            if stamp and self.object_stamps.get(dest) == stamp:
                continue
            if stat is None or sha256(read_shared_file(dest, MAX_CLIP)).hexdigest() != checksum:
                write_shared_file(dest, checked_audio(source.read_bytes(), checksum))
                if sha256(read_shared_file(dest, MAX_CLIP)).hexdigest() != checksum:
                    raise ValueError("A recording could not be verified in the chosen folder.")
                stat = dest.stat()
            self.object_stamps[dest] = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        # Every installation owns its own manifest. Concurrent saves on two
        # machines cannot overwrite each other's cards. Publish audio first.
        if not own.exists() or read_shared_file(own, MAX_INDEX) != local:
            write_shared_file(own, local)
        if read_shared_file(own, MAX_INDEX) != local:
            raise ValueError("The saved card index could not be verified.")
        fingerprints = card_fingerprints(*validate_manifest(local))
        if medium in CLOUD | MIRRORS:
            # A local mirror can accept writes while its cloud client is offline.
            # A second installation acknowledging the same card proves delivery.
            if medium in MIRRORS or not provider_confirmed([own, *(objects / (digest + ".ogg") for digest in sources)]):
                fingerprints = {k: v for k, v in fingerprints.items() if peer_cards.get(k) == v}
        receipt = {"folder": str(folder), "cards": fingerprints}
        if self.store.setting(self.sync_key + "_receipt", {}) != receipt:
            self.store.set_setting(self.sync_key + "_receipt", receipt)
        self.last_scan = None if self.ingress.active else (sha256(local).digest(), revisions)
        self.status = {"state": "synced", "added": added, "conflicts": conflicts}
