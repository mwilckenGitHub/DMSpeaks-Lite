"""Serve WoW menu requests without opening the companion interface."""

from pathlib import Path
import json
from sqlite3 import OperationalError
from threading import Event, Lock, Thread
from time import monotonic, perf_counter, time, time_ns
from zipfile import BadZipFile

from lite_storage_setup import local_location
from lite_card_transfer import CardTransfer, restore_loader
from lite_fetch_timing import dialogue_timings, history_key, npc_timings, save_pass, timing_report
from lite_name_automatic import addons_folder
from lite_name_bridge import (
    FALLBACK_RELOAD, PROVIDERS, ensure_status_addon, fetch_summary, imported_character, load_settings, publish, remember_character,
    request_order,
)
from lite_name_settings import DEFAULTS, PREFS, RECORDING_PLAYBACK, configure, personal_connection_id, test_connection
from lite_portable_inputs import SETTING as INPUTS, InputImportCancelled, import_input, imported
from lite_portable_references import LEGACY_MISSING_SPOKEN
from lite_portable_package import ADDON as PLAYBACK_ADDON
from lite_portable_package import refresh_playback_loader
from name_fetch_jobs import visible_activity
from project_recovery import WorkingCopyLease
from wow_armory_portraits import ArmoryPortraits, UNKNOWN_PORTRAIT, generic_portrait, installed_region, portrait_identity


CONSUMED = "lite_game_requests"
OPTIONS = "lite_game_options"
GAME_CHARACTER = "lite_game_character"
CATALOG_NEEDED = "Add a prepared dialogue catalog in Fetch Settings before fetching audio."
INFLIGHT = "lite_game_fetch_inflight"
LAST_FETCH = "lite_game_last_fetch"


def reload_hint(activity):
    ready, announced = activity.get("reload_ready"), activity.get("reload_at")
    if type(ready) is int and ready > 0 and isinstance(announced, (int, float)) and 0 <= time() - announced < 20:
        return f"/reload to use {ready:,} new dialogues."
    return None


def measured_progress(job):
    progress = job.get("result", {}).get("coverage_progress")
    if not progress:
        return {}
    from lite_portable_constraints import OPENING_NPCS

    zone = progress.get("zone") or {}
    first = OPENING_NPCS.get(zone.get("id"))
    if first and any(row.get("id") == first for row in zone.get("npcs", [])):
        progress = {**progress, "zone": {**zone, "opening_npc_id": first}}
    finished = job.get("finished_at_ns")
    checked_at = finished / 1_000_000_000 if finished else job.get("created")
    return {"coverage_progress": {**progress, "checked_at": checked_at}}


def coverage_summary(result):
    if count := result.get("coverage_count"):
        ready, total = count["ready"], count["total"]
        message = f"{ready:,} with saved name audio · {total - ready:,} without."
        return {**count, "already_fetched": ready, "message": message}
    statuses = result["statuses"]
    total = len(statuses)
    fetchable = sum(item["state"] == "queued" for item in statuses)
    fetched = sum(item["state"] == "staged" for item in statuses)
    held = total - fetchable - fetched
    ready = fetchable + fetched
    if not total:
        message = "The prepared catalog has no dialogues for your selection."
    else:
        message = f"{fetchable} ready to fetch · {fetched} already fetched · {held} need attention."
        reason = next((item["reason"] for item in (*result.get("placement_errors", ()), *statuses)
                       if item.get("reason")), None)
        if reason:
            message += f" {reason}"
    if result.get("fallback_reload"):
        message = FALLBACK_RELOAD + " " + message
    return {"ready": ready, "total": total, "fetchable": fetchable, "already_fetched": fetched,
            "held": held, "message": message}


def fetch_scope(job):
    if job.get("replace_recording"):
        return "replacement"
    return "selected" if job.get("source_keys") else "area"


def completed_fetch(result, *, stopped=False, pause_reason=None):
    statuses = result["statuses"]
    total = len(statuses)
    ready = result["placed_dialogues"]
    state, detail = fetch_summary(ready, statuses, result["placement_errors"], paused=result["paused"],
                                  restart_required=result.get("restart_required", False),
                                  fallback_reload=result.get("fallback_reload", False))
    if state == "paused" and stopped:
        state, detail = "stopped", "Fetch stopped. Saved recordings are available."
    elif state == "paused" and pause_reason:
        detail = pause_reason
    elif state == "unavailable" and result.get("retryable_returns", 0):
        detail = detail.replace("The previous request is held. Review it before retrying.",
                                "Start Fetch retries held takes and continues through the selected area.")
    remaining = {}
    if state == "unavailable" and ready < total:
        issues = [item for item in (*result["placement_errors"], *statuses) if item.get("reason")]
        if issues:
            actionable = next((item for item in issues if item.get("state") != "source_unavailable"), issues[0])
            remaining["remaining_reason"] = actionable["reason"]
        blocked = sum(item["state"] == "source_unavailable" for item in statuses)
        if blocked:
            remaining["source_unavailable"] = blocked
    return {"state": state, "ready": ready, "total": total, "detail": detail, **remaining,
            "elapsed_seconds": result.get("elapsed_seconds"), "retryable_returns": result.get("retryable_returns", 0)}


class GameIngress:
    def __init__(self, app, root, open_provider, *, settings_port=None, build_id=None, store_managed=False):
        self.app = app
        self.root = Path(root).resolve()
        self.addons = addons_folder(str(self.root / "Interface" / "AddOns"))
        if not (self.addons / "DMSpeaksLite" / "DMSpeaksLite.toc").is_file():
            raise ValueError("Install the DMSpeaks settings add-on in WoW before starting the background companion.")
        self.account = self.root / "WTF" / "Account"
        self.active = None
        self.progress_fetch = None
        self.resume_retry_at = 0
        self.resume_error = ""
        self.live_hud = lambda: False
        self.open_provider = open_provider
        self.settings_port = settings_port
        self.build_id = build_id
        self.store_managed = store_managed
        self.stop = Event()
        self.request_lock = Lock()
        self.coverage_lock = Lock()
        self.provider_lock = Lock()
        self.profile_lock = Lock()
        self.armory = ArmoryPortraits(app.folder / "armory-portraits")
        self.install_region = installed_region(self.root)
        self.cards = CardTransfer(self)
        self.roster_revision = None
        self.context_audio_lock = Lock()
        self.recording_revision = None
        self.recordings_by_name = {}
        self.confirmed_providers = set()
        self.preparation_lock = Lock()
        self.snapshot_lock = Lock()
        self.pending_snapshot = None
        self.catalog_lock = Lock()
        self.preparation = dict(state="preparing", stage="opening", current=0, total=0)

    def save_provider(self, data):
        with self.request_lock, self.provider_lock:
            self.finish_active()
            self.require_editable_provider(data.get("id"))
            profile = configure(self.app.connections, data)
            self.confirmed_providers.discard(profile["id"])
            return profile

    def require_editable_provider(self, ident):
        active = self.app.store.setting(INFLIGHT, {}).get("job", {})
        if ident and active.get("provider_id") == ident:
            raise ValueError("Pause or stop this fetch before changing its connection.")

    def remove_provider(self, ident):
        if not personal_connection_id(ident):
            raise ValueError("Choose a personal SSH connection to remove.")
        with self.request_lock, self.provider_lock:
            self.finish_active()
            self.require_editable_provider(ident)
            profile = self.app.connections.profile(ident)
            if (profile.get("engine"), profile.get("transport")) != ("fish_omni", "ssh"):
                raise ValueError("Choose a personal SSH connection to remove.")
            self.app.connections.remove(ident)
            self.confirmed_providers.discard(ident)
            options = self.options()
            if options.get("provider_id") == ident:
                options = {**options, "provider_id": None, "allow_metered": False}
                self.app.store.set_setting(OPTIONS, options)
                prefs = self.app.store.setting(PREFS, DEFAULTS)
                self.app.store.set_setting(PREFS, {**prefs, "provider_id": None})
            return options

    def known_provider(self, provider):
        if not isinstance(provider, str):
            return False
        if provider in PROVIDERS:
            return True
        if personal_connection_id(provider):
            profile = self.app.connections.profile(provider)
            return (profile.get("engine"), profile.get("transport")) == ("fish_omni", "ssh")
        return False

    def check_provider(self, provider, *, reuse=False):
        if not self.known_provider(provider):
            raise ValueError("Choose a saved speech provider.")
        with self.provider_lock:
            if reuse and provider in self.confirmed_providers:
                return {"ok": True}
            self.confirmed_providers.discard(provider)
            result = test_connection(self.app, provider)
            if result.get("ok") is True:
                self.confirmed_providers.add(provider)
            return result

    def start(self, plans, plans_sha256, paced=None):
        self.lease = WorkingCopyLease(self.addons, ".dmspeaks-game-ingress.lock")
        try:
            if not self.store_managed:
                refresh_playback_loader(self.addons)
            self.preparation_thread = Thread(target=self.prepare_catalog, args=(plans, plans_sha256, paced),
                                             name="dmspeaks-catalog-preparation", daemon=True)
            self.thread = Thread(target=self.watch, name="dmspeaks-game-ingress", daemon=True)
            self.card_sync_thread = Thread(target=self.cards.watch, args=(self.stop,),
                                           name="dmspeaks-card-sync", daemon=True)
            self.preparation_thread.start()
            self.thread.start()
            self.card_playback_thread = Thread(target=self.cards.watch_playback, args=(self.stop,),
                                               name="dmspeaks-share-playback", daemon=True)
            self.card_playback_thread.start()
            self.card_sync_thread.start()
        except BaseException:
            self.lease.close()
            raise

    def preparation_state(self):
        with self.preparation_lock:
            return dict(self.preparation)

    def preparation_progress(self, progress):
        with self.preparation_lock:
            self.preparation = dict(state="preparing", **progress)

    def require_ready(self):
        status = self.preparation_state()
        if status["state"] == "awaiting-catalog":
            raise ValueError(self.catalog_needed_message())
        if status["state"] != "ready":
            raise ValueError(status.get("error") or "Preparing dialogues. Fetching will be available when verification finishes.")

    def catalog_needed_message(self):
        if self.store_managed:
            return "Install or update DMSpeaks for WoW in CurseForge to get the dialogue catalog."
        return CATALOG_NEEDED

    def catalog_ready(self):
        with self.preparation_lock:
            self.preparation = dict(state="ready")

    def catalog_failed(self, error):
        with self.preparation_lock:
            self.preparation = dict(state="failed", error=str(error))
        self.report_handoff("unavailable", None, detail="Dialogue preparation failed. " + str(error))
        self.log_error("game-catalog-preparation", error)

    def submit_fetch(self, data):
        if not self.catalog_lock.acquire(blocking=False):
            raise ValueError("Preparing dialogues. Fetching will be available when verification finishes.")
        try:
            self.require_ready()
            return self.app.submit("spoken_player_names", {**data, "_game_settings_port": self.settings_port})
        finally:
            self.catalog_lock.release()

    def latest_saved_player(self):
        """Find the current character and the revision WoW saved for it."""
        selection = self.app.store.setting(GAME_CHARACTER, {})
        candidates = []
        for path in self.account.glob("*/SavedVariables/DMSpeaksLite.lua"):
            try:
                if path.resolve() != path:
                    continue
                modified_ns = path.stat().st_mtime_ns
                player = imported_character(load_settings(path))
                if player and modified_ns == path.stat().st_mtime_ns:
                    candidates.append((modified_ns, str(path) == selection.get("path"), path, player))
            except (OSError, ValueError):
                continue
        if not candidates:
            return None
        modified_ns, _, path, player = max(candidates, key=lambda item: item[:3])
        return path, player, modified_ns

    def popup_player(self):
        """Read the current character if its profile is available to Lite."""
        saved = self.latest_saved_player()
        if saved and saved[1]["id"] not in self.profile_suppressions():
            return saved[:2]
        return None

    def refresh_saved_player(self, *, manual=False):
        """Rediscover a deleted profile after WoW saves it or the user refreshes."""
        with self.profile_lock:
            saved = self.latest_saved_player()
            if not saved:
                return {}
            _, player, modified_ns = saved
            ident = player["id"]
            profiles = self.profile_suppressions()
            disposition = profiles.get(ident)
            if disposition is None:
                return {}
            mode = disposition.get("mode")
            if mode == "hide":
                return {"hidden_character": disposition} if manual else {}
            # Old Remove entries can be unhidden individually or refreshed as the current character.
            if manual or (mode == "delete" and modified_ns > disposition["deleted_ns"]):
                del profiles[ident]
                self.save_profile_suppressions(profiles)
                return {"restored": True}
            return {}

    def popup_character(self):
        linked = self.popup_player()
        if not linked:
            return None
        _, player = linked
        values = player["values"]
        return {"id": player["id"], "name": values["name"], "realm": values["realm"]}

    def name_recording_counts(self):
        """Read the index committed by the fetcher, once per revision of this game's files."""
        from lite_store_playback import INDEX, STATUS, name_audio_relative, playable_saved_names, safe_path

        revision = []
        for relative in (INDEX, STATUS / "Names.lua"):
            try:
                info = safe_path(self.addons, relative).stat()
                revision.append((info.st_mtime_ns, info.st_size, info.st_ino))
            except FileNotFoundError:
                revision.append(None)
        if revision != self.recording_revision:
            grouped = {}
            for choices in playable_saved_names(self.addons).values():
                for name, clip in choices.items():
                    path = name_audio_relative(clip)
                    grouped.setdefault(name.casefold(), set()).add(path)
            self.recordings_by_name = {name: len(paths) for name, paths in grouped.items()}
            self.recording_revision = revision
        return self.recordings_by_name

    def known_players(self, linked, recordings):
        """Include legacy profiles only when this installation has their name audio."""
        key = "lite_wow_profiles_" + str(self.root)
        known = self.app.store.setting(key, {})
        if linked:
            player = linked[1]
            previous = known.get(player["id"], {})
            values = {**previous, **player["values"]}
            if previous != values:
                known[player["id"]] = values
                self.app.store.set_setting(key, known)
        # Earlier builds stored character identities separately from their name clips.
        older = {row["id"]: row["values"] for row in self.app.store.setting("characters", [])
                 if row.get("values", {}).get("client") == "Forever"}
        return {ident: values for ident, values in older.items()
                if values.get("name", "").split(" ", 1)[0].casefold() in recordings} | known

    def profile_suppressions(self):
        """Read the saved visibility state, including old Remove entries."""
        return self.app.store.setting("lite_wow_removed_profiles_" + str(self.root), {})

    def save_profile_suppressions(self, profiles):
        """Persist visibility for this WoW installation."""
        self.app.store.set_setting("lite_wow_removed_profiles_" + str(self.root), profiles)

    def removed_profiles(self):
        """Return the profiles the user can unhide."""
        return {ident: profile for ident, profile in self.profile_suppressions().items()
                if profile.get("mode") != "delete"}

    def change_profile_removal(self, data):
        """Apply one visibility operation to the selected profiles."""
        if data == {"refresh_current": True}:
            return self.refresh_saved_player(manual=True)
        remove = data.get("removed")
        mode = data.get("mode")
        if mode is not None and (mode not in ("hide", "delete") or remove is not True):
            raise ValueError("Choose whether to hide or delete the profiles.")
        if "character_ids" in data:
            ids = data["character_ids"]
            if remove is not True or "character_id" in data:
                raise ValueError("Choose the profiles to remove.")
        else:
            ids = [data.get("character_id")]
        if (type(remove) is not bool or not isinstance(ids, list) or not ids
                or not all(isinstance(ident, str) and ident for ident in ids)):
            raise ValueError("Choose a character profile to remove or restore.")
        with self.request_lock, self.profile_lock:
            profiles = self.profile_suppressions()
            if remove:
                if self.active and self.active[4] in ids:
                    raise ValueError("Wait for this character's current fetch or coverage check to finish.")
                known = self.known_players(None, self.name_recording_counts())
                selected = {}
                deleted_ns = time_ns()
                for ident in ids:
                    profile = known.get(ident, {})
                    if not self.can_target(profile):
                        raise ValueError("That character profile is unavailable.")
                    if mode == "delete":
                        selected[ident] = {"mode": mode, "deleted_ns": deleted_ns}
                    else:
                        selected[ident] = {"name": profile["name"], "realm": profile["realm"]}
                        if mode == "hide":
                            selected[ident]["mode"] = mode
                profiles.update(selected)
            else:
                ident, = ids
                if ident not in profiles or profiles[ident].get("mode") == "delete":
                    raise ValueError("That character profile is unavailable to unhide.")
                del profiles[ident]
            self.save_profile_suppressions(profiles)
            return self.removed_profiles()

    def remember_account(self, path, ident):
        # Account folders can identify a login. Keep labels in this installation only.
        from hashlib import sha256
        key = "lite_wow_accounts_" + str(self.root)
        accounts = self.app.store.setting(key, {})
        token = sha256(path.parent.parent.name.encode("utf-8")).hexdigest()[:20]
        account = accounts.setdefault(token, {"number": len(accounts) + 1, "players": []})
        if ident not in account["players"]:
            account["players"].append(ident)
            self.app.store.set_setting(key, accounts)

    def account_labels(self, accounts, ident):
        return [{"id": key, "number": acct["number"], "name": acct.get("name", "")}
                for key, acct in accounts.items() if ident in acct["players"]]

    def rename_account(self, data):
        name = data.get("name")
        if not isinstance(name, str) or len(name) > 60 or (name and not name.isprintable()):
            raise ValueError("Use a short account label.")
        with self.profile_lock:
            key = "lite_wow_accounts_" + str(self.root)
            accounts = self.app.store.setting(key, {})
            if data.get("id") not in accounts:
                raise ValueError("Choose a WoW account on this installation.")
            accounts[data["id"]]["name"] = name.strip()
            self.app.store.set_setting(key, accounts)
        return {"saved": True}

    @staticmethod
    def can_target(values):
        return all(isinstance(values.get(k), str) and values[k] for k in ("name", "realm", "race", "class"))

    def resolve_player(self, linked, ident):
        with self.profile_lock:
            if ident in self.profile_suppressions():
                raise ValueError("That character profile is unavailable. Choose a character again.")
            if linked and ident == linked[1]["id"]:
                return linked[1]
            values = self.known_players(linked, self.name_recording_counts()).get(ident, {})
        if not self.can_target(values):
            raise ValueError("That character profile is unavailable. Choose a character again.")
        from roster import import_roster
        player = import_roster([values])[0]
        if player["id"] != ident:
            raise ValueError("The selected character identity changed. Choose a character again.")
        return player

    def armory_identity(self, ident, values, linked):
        # Existing saves predate playerRegion. The installation's current portal
        # is a reliable fallback only for the character currently linked to WoW.
        region = values.get("armory_region")
        if not region and linked and linked[1]["id"] == ident:
            region = self.install_region
        return portrait_identity(values, region)

    def character_portrait(self, ident, region):
        linked = self.popup_player()
        with self.profile_lock:
            player = self.known_players(linked, self.name_recording_counts()).get(ident, {})
            identity = self.armory_identity(ident, player, linked)
        return self.armory.image(identity) if identity and identity[0] == region else None

    def character_profiles(self):
        from lite_portable_constraints import RACE_FACTIONS
        from lite_races import profile_race

        linked = self.popup_player()
        with self.profile_lock:
            recordings = self.name_recording_counts()
            options = self.options()
            known = self.known_players(linked, recordings)
            storage = self.cards.storage_states(known)
            default_location = local_location(self.addons)
            accounts = self.app.store.setting("lite_wow_accounts_" + str(self.root), {})
            removed = self.profile_suppressions()
            rows, represented = [], set()
            for ident, values in known.items():
                name = values.get("first_name") or (values.get("name") or "").partition(" ")[0]
                audio_key = name.casefold()
                if not name:
                    continue
                represented.add(audio_key)
                if ident in removed:
                    continue
                race = profile_race(values)
                faction = values.get("faction") or RACE_FACTIONS.get(race)
                measured = self.last_coverage({"id": ident}, options)
                if measured:
                    measured = {**measured, "provider_id": options["provider_id"], "area": options["coverage"]}
                identity = self.armory_identity(ident, values, linked)
                portrait = f"/character-portraits/{identity[0]}/{ident}.jpg" if identity else None
                fallback = generic_portrait(race, values.get("gender"))
                rows.append(dict(accounts=self.account_labels(accounts, ident), ruleset=self.armory.ruleset(identity), region=values.get("armory_region") or (self.install_region if linked and linked[1]["id"] == ident else None),
                                 storage=storage.get(ident, default_location), portrait=portrait, fallback_portrait=fallback, id=ident, name=values["name"], realm=values.get("realm", ""), faction=faction,
                                 level=values.get("level"), race=race, gender=values.get("gender"),
                                 **{"class": values.get("class")}, selectable=self.can_target(values),
                                 count=recordings.get(audio_key, 0), coverage=measured,
                                 linked=bool(linked and linked[1]["id"] == ident)))
            for audio_key in recordings.keys() - represented:
                rows.append(dict(storage=storage.get("audio:" + audio_key, default_location), fallback_portrait=UNKNOWN_PORTRAIT, id="audio:" + audio_key, name=audio_key[:1].upper() + audio_key[1:], realm="",
                                 faction=None, count=recordings[audio_key], coverage=None, linked=False, selectable=False))
            return sorted(rows, key=lambda row: (not row["linked"], row["name"].casefold(), row["realm"].casefold()))

    def sync_game_roster(self):
        from lite_name_bridge import status_addon_update
        from lite_portable_io import cache_bytes
        from spoken_pack_lua import lua_string

        restore_loader(self.addons)
        paths = sorted(self.account.glob("*/SavedVariables/DMSpeaksLite.lua"))
        revision = (tuple((str(path), path.stat().st_mtime_ns, path.stat().st_size) for path in paths),
                    frozenset(self.profile_suppressions()))
        if self.roster_revision == revision:
            return
        self.refresh_saved_player()
        for path in paths:
            if path.resolve() != path:
                continue
            try:
                player = imported_character(load_settings(path))
            except ValueError:
                continue
            if player:
                with self.profile_lock:
                    self.known_players((path, player), {})
                    self.remember_account(path, player["id"])
        rows = sorted((row for row in self.character_profiles() if row["selectable"]),
                      key=lambda row: (row["name"].casefold(), row["realm"].casefold(), row["id"]))
        lines = ["DMSpeaksLiteRoster = {"]
        for row in rows:
            fields = ", ".join(key + " = " + lua_string(row[key]) for key in ("id", "name", "realm"))
            lines.append("    {" + fields + "},")
        content = ("\n".join([*lines, "}", ""])).encode()
        with status_addon_update(self.addons):
            path = self.addons / "DMSpeaksLiteStatus" / "Profiles.lua"
            if path.read_bytes() != content:
                cache_bytes(path, content)
        self.roster_revision = revision

    def popup_profile(self, data):
        linked = self.popup_player()
        path, player = linked or (None, None)
        if data.get("profile_id") is not None:
            if not isinstance(data["profile_id"], str) or data["profile_id"] != data.get("character_id"):
                raise ValueError("Choose the character to fetch for.")
            player = self.resolve_player(linked, data["profile_id"])
        elif not player:
            raise ValueError("Select a saved character card or log in to WoW to use Current character.")
        elif data.get("character_id") != player["id"]:
            raise ValueError("The WoW character changed. Reopen Fetch Settings from WoW.")
        return path, player

    def popup_selection(self, data):
        options = self.options()
        if not options.get("provider_id"):
            raise ValueError("Choose and save a speech provider before continuing.")
        path, player = self.popup_profile(data)
        if any(data.get(field) != options[field] for field in ("provider_id", "coverage")):
            raise ValueError("The saved provider or coverage changed. Reopen Fetch Settings to use the latest options.")
        return path, player, options

    def review_recordings(self, data):
        from lite_recording_review import recordings, selected_name
        with self.request_lock:
            _, player = self.popup_profile(data)
            return recordings(self.addons, selected_name(player), data.get("offset", 0))

    def playback_profile(self, data):
        if self.app.store.setting(RECORDING_PLAYBACK) is not True:
            raise ValueError("Recording playback is disabled.")
        _, player = self.popup_profile(data)
        return player

    def review_audio(self, data):
        from lite_recording_review import audio, selected_name, selection
        with self.request_lock:
            player = self.playback_profile(data)
            return audio(self.addons, selected_name(player), selection(data))

    def play_recording(self, data):
        from lite_recording_review import recording_preview, selected_name, selection
        with self.request_lock:
            _, player = self.popup_profile(data)
            return recording_preview(self.addons, selected_name(player), selection(data))

    def review_context_audio(self, data):
        from lite_recording_context import context_audio
        from lite_recording_review import current_take, selected_name, selection

        target = selection(data)
        # Serialize full-line decoding without holding the settings or fetch lock.
        with self.context_audio_lock:
            with self.request_lock:
                player = self.playback_profile(data)
            content = context_audio(self.addons, player, target)
            with self.request_lock:
                if self.playback_profile(data) != player:
                    raise ValueError("The character changed. Choose the recording again.")
                current_take(self.addons, selected_name(player), target)
            return content

    def start_coverage(self, data):
        with self.request_lock:
            _, player, options = self.popup_selection(data)
            remember_character(self.app.store, player)
            job = self.submit_fetch({"character_id": player["id"], "coverage": options["coverage"],
                                     "provider_id": options["provider_id"], "addons_folder": str(self.addons),
                                     "submit": False, "quick_coverage": True, "_keep_fetch_preferences": True})
        return {"job_id": job["id"], "character": player["values"]["name"], "coverage": options["coverage"]}

    def coverage_key(self, character_id, provider, area):
        return "lite_coverage_" + json.dumps([str(self.root), character_id, provider, area], separators=(",", ":"))

    def last_coverage(self, character, options):
        if character and options.get("provider_id") and options.get("coverage"):
            key = self.coverage_key(character["id"], options["provider_id"], options["coverage"])
            return self.app.store.setting(key, None)
        return None

    def remember_coverage(self, job, count=None):
        if count is None:
            count = job["result"].get("coverage_count")
        scope = job.get("coverage_scope")
        if not count or not scope or scope[2] is not None or not job.get("character_id"):
            return
        key = self.coverage_key(job["character_id"], scope[0], scope[1])
        progress = measured_progress(job)
        checked_at = progress.get("coverage_progress", {}).get("checked_at") or job["created"]
        measured = dict(ready=count["ready"], total=count["total"], checked_at=checked_at, **progress)
        with self.coverage_lock:
            previous = self.app.store.setting(key, {})
            if previous.get("checked_at", 0) < measured["checked_at"]:
                self.app.store.set_setting(key, measured)

    def coverage_status(self, job_id):
        if not isinstance(job_id, str) or not job_id:
            raise ValueError("Choose a coverage check.")
        job = self.app.job(job_id)
        if job.get("request_mode") != "coverage":
            raise ValueError("Choose a coverage check.")
        state = job["status"]
        if state == "complete":
            self.remember_coverage(job)
            progress = measured_progress(job)
            checked_at = progress.get("coverage_progress", {}).get("checked_at") or job["created"]
            return {"state": state, "checked_at": checked_at, **coverage_summary(job["result"]), **progress}
        if state == "failed":
            raise ValueError(job.get("error") or "The coverage check could not start.")
        return {"state": state}

    def fetch_errors_key(self, character_id, options):
        return "lite_fetch_errors_" + self.coverage_key(character_id, options["provider_id"], options["coverage"])

    def saved_fetch_issues(self, character_id, options):
        if not options.get("provider_id") or not options.get("coverage"):
            return {}
        return self.app.store.setting(self.fetch_errors_key(character_id, options), {})

    def reference_check_id(self):
        catalog = self.app.store.setting(INPUTS, {}).get("plans", {}).get("sha256", "")
        return "stock-or-default:" + catalog

    def fetch_issues(self, character_id, options):
        saved = self.saved_fetch_issues(character_id, options)
        return self.current_fetch_issues(saved)

    def current_fetch_issues(self, saved):
        result = dict(saved)
        current = None
        for key, item in saved.items():
            old_native = (key.startswith("native:") and item.get("state", "held") == "held"
                          and item.get("reason") == LEGACY_MISSING_SPOKEN)
            if not old_native and item.get("state") != "source_unavailable":
                continue
            current = current or self.reference_check_id()
            if item.get("reference_check") != current:
                label = item.get("npc") or "this dialogue"
                source = item.get("source_id", "").split(":")
                if len(source) == 3 and source[0] == "q" and source[1].isdigit():
                    label += f" (quest {source[1]})"
                result[key] = {**item, "state": "source_recheck",
                               "reason": f"Start Fetch to check for a matching default voice reference for {label}."}
        return result

    def fetch_errors(self, character_id, options):
        return {key: item for key, item in self.fetch_issues(character_id, options).items()
                if item.get("state") != "source_unavailable"}

    def remember_fetch_errors(self, job, result):
        key = self.fetch_errors_key(job["character_id"], job)
        errors = self.app.store.setting(key, {})
        for item in result["statuses"]:
            source = item.get("source_key")
            if not source:
                continue
            if item.get("reason"):
                errors[source] = {field: item[field] for field in ("source_key", "source_id", "npc", "state", "reason", "retake_attempts")
                                  if field in item}
                if item.get("state") == "source_unavailable":
                    errors[source]["reference_check"] = self.reference_check_id()
            elif item["state"] == "staged" and not item.get("final_delivery_pending"):
                errors.pop(source, None)
        self.app.store.set_setting(key, errors)

    def popup_fetch(self, data):
        action, owner_id, job_id = (data.get(key) for key in ("action", "character_id", "job_id"))
        if action not in ("start", "status", "pause", "stop", "review", "timing") or not isinstance(owner_id, str) or not owner_id:
            raise ValueError("Choose a fetch action and a WoW character.")
        if job_id is not None and (not isinstance(job_id, str) or not job_id):
            raise ValueError("Choose an audio fetch.")
        with self.request_lock:
            if action == "timing":
                options = self.options()
                if any(data.get(field) != options.get(field) for field in ("provider_id", "coverage")):
                    raise ValueError("The fetch selection changed. Reopen its timing report.")
                key = history_key(self.root, owner_id, options.get("provider_id"), options.get("coverage"))
                request_id = data.get("request_id")
                rows = dialogue_timings(self.app.store, key, request_id)
                saved = self.app.store.setting(LAST_FETCH, {})
                if (saved.get("root") == str(self.root) and saved.get("character_id") == owner_id
                        and saved.get("request_id") == request_id
                        and all(saved.get(field) == options.get(field) for field in ("provider_id", "coverage"))):
                    rechecks = self.source_rechecks_for_pass(saved, options, rows)
                    rows = [{**row, "state": "source_unavailable"}
                            if row.get("state") == "held" and row.get("source_key") in rechecks else row for row in rows]
                return {"dialogues": rows, "npc_times": npc_timings(rows)}
            if action == "review":
                _, player, options = self.popup_selection(data)
                issues = self.fetch_issues(player["id"], options).values()
                return {"errors": [item for item in issues if item.get("state") != "source_unavailable"],
                        "source_unavailable": [item for item in issues if item.get("state") == "source_unavailable"]}
            if action == "start":
                retry_held = data.get("retry_held_returns", False)
                if type(retry_held) is not bool:
                    raise ValueError("Confirm the retry before sending another provider request.")
                path, player, options = self.popup_selection(data)
                replacement = None
                if "replace_recording" in data or "confirmed_replace" in data:
                    from lite_recording_review import selected_name, selection, current_take
                    if data.get("confirmed_replace") is not True or retry_held:
                        raise ValueError("Confirm the replacement of this recording.")
                    if "source_keys" in data:
                        raise ValueError("Choose one recording replacement or a dialogue retry.")
                    replacement = selection(data.get("replace_recording"))
                    current_take(self.addons, selected_name(player), replacement)
                if data.get("allow_metered") is not options["allow_metered"]:
                    raise ValueError("The saved account setting changed. Reopen Fetch Settings before fetching.")
                self.tick()
                source_keys = data.get("source_keys")
                previous = self.last_fetch_status(owner_id) if job_id is not None else {}
                if "source_keys" in data:
                    errors = self.fetch_errors(player["id"], options)
                    if (not isinstance(source_keys, list) or not source_keys
                            or any(not isinstance(key, str) or key not in errors for key in source_keys)
                            or len(set(source_keys)) != len(source_keys)):
                        raise ValueError("Choose dialogues from the current fetch error list.")
                elif not replacement and previous.get("state") == "paused" and previous.get("job_id") == job_id:
                    source_keys = self.app.store.setting(LAST_FETCH, {}).get("resume_job", {}).get("source_keys")
            if action in ("pause", "stop"):
                self.finish_active()
            active = self.active
            owner_job = active[0] if active and active[2] == "fetch" and active[4] == owner_id else None
            if action == "start":
                if owner_job:
                    running = self.app.store.setting(INFLIGHT, {}).get("job", {})
                    if (running.get("replace_recording") != replacement
                            or running.get("source_keys") != source_keys
                            or any(running.get(key) != options[key] for key in ("provider_id", "coverage"))):
                        raise ValueError("A different fetch is running for this character. Try again when it finishes.")
                    return self.popup_fetch_status(owner_job, owner_id)
                if job_id is not None and (previous.get("state") != "paused" or previous.get("job_id") != job_id):
                    raise ValueError("This fetch is no longer paused. Check its current status before starting another fetch.")
                if active:
                    raise ValueError(f"{active[5]['name']} already has a request in progress. Try again when it finishes.")
                if self.app.store.setting(INFLIGHT, {}).get("root") == str(self.root):
                    raise ValueError("Lite is restoring the previous fetch. Try again when it finishes.")
                remember_character(self.app.store, player)
                seconds, tick = divmod(time_ns(), 10**9)
                request = f"{seconds}:{tick}"
                job_id = self.begin_fetch(path, player, player["values"], options, request, "fetch", popup=True,
                                          profile_id=data.get("profile_id"), retry_held=retry_held, replacement=replacement,
                                          source_keys=source_keys)
                return self.popup_fetch_status(job_id, owner_id)
            if action in ("pause", "stop"):
                if not job_id:
                    raise ValueError("Choose an audio fetch.")
                # A request from an older tab cannot control a newer job.
                if owner_job and job_id in (owner_job, active[1]):
                    self.pause_fetch(owner_job, stopped=(action == "stop"))
                elif not active:
                    pending = self.recovering_fetch()
                    if (pending and pending["request_id"] == job_id
                            and pending["job"]["character_id"] == owner_id):
                        stopped = pending.get("stop_requested") is True or action == "stop"
                        self.app.store.set_setting(INFLIGHT, {**pending, "pause_requested": True,
                                                               "stop_requested": stopped})
                        self.recover_fetch()
                        return self.popup_fetch_status(None, owner_id)
                    previous = self.last_fetch_status(owner_id)
                    if action == "stop" and previous["state"] == "paused" and previous.get("job_id") == job_id:
                        outcome = {**previous, "state": "stopped",
                                   "detail": "Fetch stopped. Saved recordings are available."}
                        record = self.app.store.setting(LAST_FETCH, {})
                        updated = {**record, "status": outcome}
                        save_pass(self.app.store, updated)
                        self.app.store.set_setting(LAST_FETCH, updated)
                        self.report_handoff("stopped", record.get("request_id"), character=record.get("character"),
                                            ready=outcome.get("ready", 0), total=outcome.get("total", 0),
                                            detail=outcome["detail"])
                        return self.with_fetch_timing(owner_id, outcome)
            if active and not owner_job:
                return self.with_fetch_timing(owner_id, {"state": "busy", "detail": f"{active[5]['name']} already has a request in progress."})
            return self.popup_fetch_status(owner_job, owner_id)

    def with_fetch_timing(self, owner_id, status):
        options = self.options()
        key = history_key(self.root, owner_id, options.get("provider_id"), options.get("coverage"))
        last = self.app.store.setting(LAST_FETCH, {})
        return {**status, "timing": timing_report(self.app.store, key, last)}

    def popup_fetch_status(self, job_id, owner_id):
        result = self._popup_fetch_status(job_id, owner_id)
        if (self.active and self.active[0] == job_id
                and result["state"] in ("complete", "preview", "unavailable", "paused", "stopped")):
            # The UI may finish polling before the watcher's next tick.
            self.finish_active()
        return self.with_fetch_timing(owner_id, result)

    def _popup_fetch_status(self, job_id, owner_id):
        review_count = len(self.fetch_errors(owner_id, self.options()))
        if job_id is None:
            pending = self.recovering_fetch()
            if pending:
                if pending["job"]["character_id"] != owner_id:
                    return {"state": "busy", "detail": f"{pending['character']['name']} has a fetch waiting to resume."}
                detail = "Resuming fetch."
                if self.resume_error:
                    detail = "Waiting to resume fetch. " + self.resume_error
                return {"state": "queued", "job_id": pending["request_id"], "detail": detail,
                        "review_count": review_count}
            return {**self.last_fetch_status(owner_id), "review_count": review_count}
        job = self.app.job(job_id)
        if job.get("request_mode") != "fetch" or job.get("character_id") != owner_id:
            raise ValueError("Choose an audio fetch for this character.")
        state = job["status"]
        result = {"job_id": job_id, "state": state, "started_at": job.get("created"), "review_count": review_count}
        if state == "failed":
            return {**result, "state": "unavailable", "detail": job.get("error") or "The fetch could not start."}
        activity, completing = visible_activity(job)
        saved = self.app.store.setting(INFLIGHT, {}) if self.active and self.active[0] == job_id else {}
        if saved:
            result["fetch_scope"] = fetch_scope(saved["job"])
            if saved.get("accepted_at_ns"):
                result["started_at"] = saved["accepted_at_ns"] / 1_000_000_000
        if result.get("started_at") is not None:
            end = job["finished_at_ns"] / 1_000_000_000 if job.get("finished_at_ns") else time()
            result["pass_elapsed_seconds"] = max(0, end - result["started_at"])
        if state == "complete" and not completing:
            if not saved:
                previous = self.last_fetch_status(owner_id)
                if previous.get("job_id") == job_id:
                    return {**result, **previous}
            return {**result, **completed_fetch(job["result"], stopped=saved.get("stop_requested") is True,
                                                pause_reason=saved.get("pause_reason")),
                    **measured_progress(job)}
        if completing:
            result["state"] = "running"
        progress = job.get("progress", {})
        detail = activity.get("detail")
        if not detail:
            detail = {"fetching": "Waiting for the speech provider.", "aligning": "Checking the join."}.get(
                activity.get("phase"))
        if npc := activity.get("npc"):
            if activity.get("npc_started_at") is not None:
                sampled = activity["npc_sampled_at"] if completing else perf_counter()
                result["npc_time"] = {"npc": npc, "elapsed_seconds": max(0, sampled - activity["npc_started_at"])}
            dialogue = f"{npc} · {activity.get('stage') or 'Dialogue'}"
            occurrence = activity.get("name_occurrence")
            if occurrence:
                dialogue += f" · Name {occurrence} of {activity['name_total']}"
            detail = f"{dialogue}. {detail}" if detail else dialogue
        elif not detail:
            detail = job.get("message")
        if detail:
            result["detail"] = detail
        if activity.get("excerpt"):
            result["dialogue_excerpt"] = activity["excerpt"]
        if hint := reload_hint(activity):
            result["reload_hint"] = hint
        if saved.get("pause_requested") is True:
            result["stopping"] = saved.get("stop_requested") is True
            result["pausing"] = not result["stopping"]
            if result["pausing"] and saved.get("pause_reason"):
                result["detail"] = saved["pause_reason"]
        for key in ("ready", "total"):
            result[key] = max(progress.get(key, 0), activity.get(key, 0))
        for key in ("dialogue_number", "instance_progress", "npc_number", "npc_total",
                    "coverage_progress", "retake_attempt", "retake_total"):
            if activity.get(key) is not None:
                result[key] = activity[key]
        return result

    def progress_display(self):
        """Read the real player job for the non-interactive desktop HUD."""
        active = self.progress_fetch
        if active is None:
            return None
        job_id, _, _, path, _, owner, _ = active
        job = self.app.job(job_id)
        activity, completing = visible_activity(job)
        if job["status"] not in ("queued", "running") and not completing:
            return None
        saved = {}
        if path is not None:
            try:
                saved = load_settings(path)
            except (OSError, ValueError):
                pass
        if saved.get("game_progress") is False:
            return None
        mode = saved.get("progress_mode") or self.app.store.setting(PREFS, DEFAULTS).get("progress", "bar")
        if mode == "off":
            return None
        style = {"corner": "BOTTOMRIGHT", "opacity": 85, "minimized": False, **saved.get("hud", {})}
        if mode == "compact":
            style["minimized"] = True
        title = f"{owner['name']} · {activity['npc']}" if activity.get("npc") else owner["name"]
        number = activity.get("dialogue_number")
        count = f"NPC {activity['npc_number']:,} of {activity['npc_total']:,}" if activity.get("npc_number") else ""
        occurrence = f"Dialogue {number:,} of {activity['total']:,}" if number else ""
        detail = activity.get("detail") or {
            "fetching": "Requesting name audio.",
            "aligning": "Checking name audio.",
        }.get(activity.get("phase"), "Preparing the dialogue.")
        zone = activity.get("coverage_progress", {}).get("zone")
        if zone:
            ready, total = zone["ready"], zone["total"]
            count = f"{ready:,}/{total:,} ready"
            title = f"{zone['label']} · {count}" if style["minimized"] else zone["label"]
            npc = activity.get("npc")
            detail = f"{npc} · {detail}" if npc else detail
            attempt = activity.get("retake_attempt")
            occurrence = f"Retry {attempt}/{activity['retake_total']}" if attempt else ""
            return {**style, "title": title, "count": count, "occurrence": occurrence, "detail": detail,
                    "percent": 100 * ready / total if total else None, "working": not completing,
                    "reload_hint": reload_hint(activity)}
        return {**style, "title": title, "count": count, "occurrence": occurrence, "detail": detail,
                "percent": activity.get("instance_progress"), "working": not completing,
                "reload_hint": reload_hint(activity)}

    def source_rechecks_for_pass(self, saved, options, timings=None):
        status = saved["status"]
        if status["state"] != "unavailable":
            return {}
        owner = saved["character_id"]
        previous = self.saved_fetch_issues(owner, options)
        rechecks = {key: (previous[key].get("reason"), issue["reason"])
                    for key, issue in self.current_fetch_issues(previous).items() if issue.get("state") == "source_recheck"}
        if not rechecks:
            return {}
        if timings is None and saved.get("request_id"):
            key = history_key(self.root, owner, options.get("provider_id"), options.get("coverage"))
            timings = dialogue_timings(self.app.store, key, saved["request_id"])
        if timings:
            held = {row.get("source_key") for row in timings if row.get("state") in ("held", "source_unavailable")}
            return {key: messages for key, messages in rechecks.items() if key in held}
        selected = saved.get("resume_job", {}).get("source_keys")
        return {key: messages for key, messages in rechecks.items()
                if (not selected or key in selected) and status.get("remaining_reason") == messages[0]}

    def last_fetch_status(self, owner_id):
        saved = self.app.store.setting(LAST_FETCH, {})
        options = self.options()
        if (saved.get("root") == str(self.root)
                and saved.get("character_id") == owner_id and options
                and all(saved.get(key) == options[key] for key in ("provider_id", "coverage"))):
            status = saved["status"]
            if rechecks := self.source_rechecks_for_pass(saved, options):
                status = dict(status)
                if "source_unavailable" in status:
                    status["source_unavailable"] = max(0, status["source_unavailable"] - len(rechecks))
                for previous, current in rechecks.values():
                    if previous and status.get("remaining_reason") == previous:
                        status["remaining_reason"] = current
                    if previous and status.get("detail"):
                        status["detail"] = status["detail"].replace(previous, current)
            if "fetch_scope" not in status and saved.get("resume_job", {}).get("source_keys"):
                status = {**status, "fetch_scope": "selected"}
            if status["state"] == "paused" and not status.get("job_id"):
                return {**status, "job_id": f"paused-{saved.get('finished_at_ns', 0)}"}
            finished = saved.get("finished_at_ns")
            if (status.get("fetch_scope") == "area" and status["state"] in ("complete", "preview", "unavailable")
                    and type(status.get("ready")) is int and type(status.get("total")) is int
                    and type(finished) is int and finished > 0):
                checked = self.last_coverage({"id": owner_id}, options)
                if (checked and type(checked.get("checked_at")) in (int, float)
                        and checked["checked_at"] > finished / 1_000_000_000
                        and type(checked.get("ready")) is int and type(checked.get("total")) is int
                        and 0 <= checked["ready"] <= checked["total"]
                        and (checked["ready"], checked["total"]) != (status.get("ready"), status.get("total"))):
                    return {**status, "newer_area_coverage": checked}
            return status
        return {"state": "idle"}

    def remember_fetch(self, record, status, finished_at_ns=None, statuses=()):
        job = record["job"]
        if finished_at_ns and record.get("accepted_at_ns") and finished_at_ns >= record["accepted_at_ns"]:
            status = {**status, "elapsed_seconds": (finished_at_ns - record["accepted_at_ns"]) / 1_000_000_000}
        saved = {
            "root": str(self.root), "character_id": job["character_id"], "provider_id": job["provider_id"],
            "coverage": job["coverage"], "status": {**status, "fetch_scope": fetch_scope(job)}, "path": record.get("path"),
            "character": record.get("character"), "accepted_at_ns": record.get("accepted_at_ns"),
            "request_id": record.get("request_id"),
            "finished_at_ns": finished_at_ns,
            "resume_job": {"source_keys": job["source_keys"]} if "source_keys" in job else {},
        }
        # Preserve the last pass written by builds that predate timing history.
        save_pass(self.app.store, self.app.store.setting(LAST_FETCH, {}))
        save_pass(self.app.store, saved, statuses)
        self.app.store.set_setting(LAST_FETCH, saved)

    def pause_fetch(self, job_id, *, stopped=False, reason=None):
        record = self.app.store.setting(INFLIGHT, {})
        if record and self.active and record.get("request_id") == self.active[1]:
            if reason and record.get("pause_requested"):
                return
            self.app.store.set_setting(INFLIGHT, {**record, "pause_requested": True,
                "stop_requested": stopped or record.get("stop_requested") is True,
                **({"pause_reason": reason} if reason else {})})
        self.app.pause_player_name_fetch(job_id)

    def replace_catalog(self, payload, *, importer):
        if not self.catalog_lock.acquire(blocking=False):
            raise ValueError("Preparing dialogues. Wait for verification to finish before importing another input.")
        try:
            previous = self.preparation_state()["state"]
            if previous == "preparing":
                raise ValueError("Preparing dialogues. Wait for verification to finish before importing another input.")
            with self.app.lock:
                if any(job["operation"] == "spoken_player_names" and job["status"] in {"queued", "running"}
                       for job in self.app.jobs.values()):
                    raise ValueError("Wait for the current coverage check or fetch to finish before replacing its dialogue catalog.")
            previous_plans = self.app.store.setting(INPUTS, {}).get("plans")
            self.preparation_progress(dict(stage="opening", current=0, total=0))
            try:
                self.report_handoff("planning", None,
                                    detail="Preparing replacement dialogues. Fetching will resume after verification.")
                result = importer(self.app, payload, progress=self.preparation_progress, should_stop=self.stop.is_set)
            except Exception as error:
                self.recover_catalog_after_rejection(previous, previous_plans, error)
                raise
            self.catalog_ready()
            self.report_handoff("connected", None, detail="The replacement dialogue catalog is verified and ready.")
            return result
        finally:
            self.catalog_lock.release()

    def import_playback(self, payload):
        from lite_paced_delivery import import_and_package

        if not self.catalog_lock.acquire(blocking=False):
            raise ValueError("Wait for the current dialogue verification before importing playback.")
        try:
            with self.app.lock:
                if any(job["operation"] == "spoken_player_names" and job["status"] in {"queued", "running"}
                       for job in self.app.jobs.values()):
                    raise ValueError("Wait for the current coverage check or fetch before importing playback.")
            previous = self.preparation_state()
            if previous["state"] == "preparing":
                raise ValueError("Wait for the current dialogue verification before importing playback.")
            self.preparation_progress(dict(stage="checking playback archive", current=0, total=0))
            toc = self.addons / PLAYBACK_ADDON / f"{PLAYBACK_ADDON}.toc"
            first_install = not toc.is_file()
            try:
                result = import_and_package(
                    self.app, dict(path=payload.get("path"), sha256=payload.get("sha256"),
                                   input_sha256=payload.get("input_sha256"), addons_folder=str(self.addons)),
                    progress=self.preparation_progress,
                )
            except (AttributeError, IndexError, KeyError, TypeError) as error:
                raise ValueError("The verified playback collection has invalid metadata.") from error
            finally:
                with self.preparation_lock:
                    self.preparation = previous
            changed = result["placement_changed"]
            if changed:
                next_step = ("Restart WoW to load the playback add-on." if first_install
                             else "Reload WoW to use the verified playback collection.")
                self.report_handoff("connected", None, detail=next_step)
            else:
                next_step = "This playback collection is already installed."
            return dict(count=result["packaged"], placement_changed=changed, message=next_step)
        finally:
            self.catalog_lock.release()

    def recover_catalog_after_rejection(self, previous, plans, error):
        current = self.app.store.setting(INPUTS, {}).get("plans")
        if previous == "awaiting-catalog" and current is None:
            with self.preparation_lock:
                self.preparation = dict(state="awaiting-catalog")
            detail = CATALOG_NEEDED
        elif previous == "ready" and plans and current == plans:
            try:
                imported(self.app, "plans")
            except (OSError, ValueError, BadZipFile):
                self.catalog_failed(error)
                return
            self.catalog_ready()
            detail = "The replacement catalog was rejected. The current dialogue catalog is still ready."
        else:
            self.catalog_failed(error)
            return
        self.report_handoff("connected", None, detail=detail)
        self.log_error("game-catalog-preparation", error)

    def prepare_catalog(self, plans, plans_sha256, paced=None):
        self.catalog_lock.acquire()
        completed_state = None
        try:
            if plans is None and (self.store_managed or not self.app.store.setting(INPUTS, {}).get("plans")):
                self.report_handoff("connected", None, detail=self.catalog_needed_message())
                completed_state = "awaiting-catalog"
                return
            self.report_handoff("planning", None, detail="Preparing dialogues. You can set up your provider while verification finishes.")
            if plans:
                import_input(self.app, dict(kind="plans", path=str(Path(plans).resolve()), sha256=plans_sha256),
                             progress=self.preparation_progress, should_stop=self.stop.is_set,
                             build_id=self.build_id, installed_addon=self.store_managed)
            else:
                imported(self.app, "plans", progress=self.preparation_progress, should_stop=self.stop.is_set,
                         build_id=self.build_id)
            if self.stop.is_set():
                return
            if paced:
                from lite_paced_delivery import import_and_package
                self.preparation_progress(dict(stage="checking playback archive", current=0, total=0))
                import_and_package(
                    self.app, {**paced, "addons_folder": str(self.addons)},
                    progress=self.preparation_progress, build_id=self.build_id,
                )
                if self.stop.is_set():
                    return
            self.report_handoff("connected", None, detail="Choose Check Coverage or Fetch Names in DMSpeaks.")
            completed_state = "ready"
        except InputImportCancelled:
            return
        except Exception as error:
            self.catalog_failed(error)
        finally:
            # Readers must see the final state only after input operations can acquire the catalog lock.
            with self.preparation_lock:
                self.catalog_lock.release()
                if completed_state is not None:
                    self.preparation = dict(state=completed_state)

    def close(self):
        self.stop.set()
        self.thread.join()
        self.card_sync_thread.join()
        self.card_playback_thread.join()
        self.preparation_thread.join()
        self.lease.close()

    def watch(self):
        content_checked = False
        while not self.stop.is_set():
            if not content_checked:
                try:
                    ensure_status_addon(self.addons, refresh_content=True)
                    content_checked = True
                except (OSError, ValueError) as error:
                    self.log_error("game-ingress", error)
            try:
                with self.request_lock:
                    self.tick()
                with self.snapshot_lock:
                    self.publish_pending_snapshot()
            except (OSError, ValueError, OperationalError) as error:
                self.log_error("game-ingress", error)
            self.stop.wait(1)

    def log_error(self, operation, error):
        try:
            self.app.activity.append("error", str(error), {"operation": operation})
        except OSError:
            # A locked or full journal must not terminate the fetch/recovery watcher.
            pass

    def report(self, state, request=None, **fields):
        options = self.app.store.setting(OPTIONS, {})
        with self.snapshot_lock:
            self.pending_snapshot = dict(state=state, request_id=request, background=True,
                                         provider=options.get("provider_id"), coverage=options.get("coverage"),
                                         settings_port=self.settings_port, live_hud=self.live_hud(), **fields)
            self.publish_pending_snapshot()

    def publish_pending_snapshot(self):
        # Only the latest status is useful after another process releases the add-on.
        if self.pending_snapshot is not None:
            publish(self.addons, **self.pending_snapshot)
            self.pending_snapshot = None

    def report_handoff(self, state, request, **fields):
        """A snapshot write may briefly conflict with the client's UI reload."""
        try:
            self.report(state, request, **fields)
        except (OSError, ValueError, OperationalError) as error:
            self.log_error("game-ingress", error)

    def options(self, data=None):
        if data is None:
            return {"coverage": DEFAULTS["coverage"], **self.app.store.setting(OPTIONS, {})}
        provider, metered = (data.get(field) for field in ("provider_id", "allow_metered"))
        coverage = data.get("coverage", DEFAULTS["coverage"])
        if (not self.known_provider(provider)
                or coverage not in ("starting-area", "route", "all") or type(metered) is not bool):
            raise ValueError("Choose a valid provider and fetch coverage.")
        options = dict(provider_id=provider, coverage=coverage, allow_metered=metered)
        self.app.store.set_setting(OPTIONS, options)
        # The full editor and the WoW popup use the same provider/coverage.
        prefs = {**DEFAULTS, **self.app.store.setting(PREFS, DEFAULTS),
                 'provider_id': provider, 'coverage': coverage}
        self.app.store.set_setting(PREFS, prefs)
        if not self.active and self.preparation_state()["state"] == "ready":
            self.report("connected", detail="Fetch options saved. Return to WoW to check coverage or fetch audio.")
        return options

    def consume_legacy_requests(self):
        consumed = self.app.store.setting(CONSUMED, {})
        previous = dict(consumed)
        for path in self.account.glob("*/SavedVariables/DMSpeaksLite.lua"):
            try:
                if path.resolve() == path:
                    request = load_settings(path).get("fetch_request")
                    if request_order(request) > request_order(consumed.get(str(path))):
                        consumed[str(path)] = request
            except (OSError, ValueError):
                # A reload can rewrite the file while it is being read.
                continue
        if consumed != previous:
            self.app.store.set_setting(CONSUMED, consumed)

    def finish_active(self):
        if not self.active:
            return None
        job_id, fetch_request, job_action, owner_path, _, owner, pause_request = self.active
        job = self.app.job(job_id)
        if job["status"] in ("queued", "running"):
            return self.active
        record = self.app.store.setting(INFLIGHT, {}) if job_action == "fetch" else {}
        reply_to = pause_request or record.get("joined_game_request") or fetch_request
        outcome = self.finish(job, reply_to, job_action, character=owner,
                              stopped=record.get("stop_requested") is True,
                              pause_reason=record.get("pause_reason"))
        if outcome is not None:
            outcome["job_id"] = job_id
        # A second menu can join a running fetch. Its reply ID changes, but the
        # original request still owns the character card and its coverage count.
        if (job_action == "fetch" and self.store_managed
                and job["status"] == "complete" and not job["result"]["paused"]
                and record.get("root") == str(self.root) and record.get("request_id") == fetch_request):
            selection = record["job"]
            area_coverage = job["result"].get("area_coverage")
            if area_coverage or fetch_scope(selection) == "area":
                scope = (selection["provider_id"], selection["coverage"], None)
                self.remember_coverage({**job, "coverage_scope": scope},
                                       area_coverage or {"ready": outcome["ready"], "total": outcome["total"]})
        if job_action == "fetch":
            if record.get("request_id") == fetch_request and outcome:
                if job["status"] == "complete" and "replace_recording" not in record["job"]:
                    self.remember_fetch_errors(record["job"], job["result"])
                self.remember_fetch(record, outcome, job.get("finished_at_ns"), job.get("result", {}).get("statuses", ()))
            if record.get("origin") != "popup" and owner_path is not None:
                self.consume(owner_path, fetch_request)
            self.app.store.set_setting(INFLIGHT, {})
        self.active = None
        if job_action == "fetch":
            self.cards.finish_fetch_sync()
        return None

    def tick(self):
        self.finish_active()
        if self.active is None and self.preparation_state()["state"] == "ready":
            self.recover_fetch()
        try:
            self.sync_game_roster()
            self.consume_legacy_requests()
        except (OSError, ValueError, OperationalError) as error:
            # A roster or SavedVariables file can be locked throughout a fetch.
            self.log_error("game-roster", error)

    def consume(self, path, request):
        consumed = self.app.store.setting(CONSUMED, {})
        if request_order(request) > request_order(consumed.get(str(path))):
            self.app.store.set_setting(CONSUMED, {**consumed, str(path): request})

    def accepted_fetch(self, path, request):
        record = self.app.store.setting(INFLIGHT, {})
        return (record.get("root"), record.get("path"), record.get("request_id")) == (
            str(self.root), str(path) if path is not None else None, request)

    def recovering_fetch(self):
        if self.active is not None:
            return {}
        record = self.app.store.setting(INFLIGHT, {})
        return record if record.get("root") == str(self.root) else {}

    def recover_fetch(self):
        """Restore the saved batch after a listener restart."""
        record = self.recovering_fetch()
        if not record:
            return False
        job = record["job"]
        request = record["request_id"]
        path = None
        if record.get("origin") != "popup" and record.get("path"):
            saved_path = Path(record["path"])
            if saved_path.is_relative_to(self.account) and saved_path.resolve() == saved_path:
                self.consume(saved_path, request)
                path = saved_path
        replacement = "replace_recording" in job
        stopped = record.get("stop_requested") is True or replacement
        if not stopped and record.get("pause_requested") is not True:
            if monotonic() < self.resume_retry_at:
                return False
            owner = record["character"]
            try:
                self.cards.reserve_fetch(owner)
                connection = self.check_provider(job["provider_id"], reuse=True)
                if connection.get("ok") is not True:
                    raise ValueError(connection.get("message") or "Waiting for the saved speech provider.")
                restored = self.submit_fetch({**job, "addons_folder": str(self.addons),
                                             "_resume_interrupted_fetch": True})
            except (OSError, ValueError) as error:
                self.resume_retry_at = monotonic() + 15
                self.resume_error = str(error)
                self.log_error("fetch-recovery", error)
                return False
            self.resume_retry_at = 0
            self.resume_error = ""
            self.active = restored["id"], request, "fetch", path, job["character_id"], owner, None
            self.progress_fetch = self.active
            self.report_handoff("planning", request, detail="Resuming fetch.", character=owner)
            return True
        if replacement:
            detail = "Review the recording again to replace it."
        elif stopped:
            detail = "Fetch stopped. Saved recordings are available."
        else:
            detail = record.get("pause_reason") or "Fetch paused. Choose Resume Fetch to continue."
        outcome = {"state": "stopped" if stopped else "paused", "job_id": request, "detail": detail}
        self.remember_fetch(record, outcome)
        self.app.store.set_setting(INFLIGHT, {})
        self.resume_retry_at = 0
        self.resume_error = ""
        self.cards.finish_fetch_sync()
        self.report_handoff(outcome["state"], request, detail=detail, character=record["character"])
        return True

    def begin_fetch(self, path, player, owner, options, request, action, *, popup=False, profile_id=None,
                    retry_held=False, replacement=None, source_keys=None):
        self.require_ready()
        if not self.app.store.setting(INPUTS, {}).get("plans"):
            raise ValueError("The background worker needs a prepared dialogue catalog.")
        payload = {
            "character_id": player["id"], "coverage": options["coverage"], "provider_id": options["provider_id"],
            "addons_folder": str(self.addons), "submit": action == "fetch", "game_request_id": request,
            "allow_metered": options["allow_metered"], "resume_provider": action == "fetch",
        }
        if replacement:
            payload["replace_recording"] = replacement
            payload["confirmed_replace"] = True
        if retry_held:
            payload["retry_held_returns"] = True
        elif popup and action == "fetch" and not replacement:
            payload["fetch_held_with_missing"] = True
        if source_keys:
            payload["source_keys"] = source_keys
            payload["retry_reviewed_dialogues"] = True
        if action == "check":
            payload.update(quick_coverage=True, _keep_fetch_preferences=True)
        if popup or profile_id:
            payload["_keep_fetch_preferences"] = True
        if action == "fetch":
            try:
                self.cards.reserve_fetch(owner)
                connection = self.check_provider(options["provider_id"], reuse=True)
                if connection.get("ok") is not True:
                    raise ValueError(connection.get("message") or "The saved speech provider did not confirm the connection.")
                self.app.store.set_setting(INFLIGHT, dict(root=str(self.root), path=str(path) if path is not None else None, request_id=request,
                                                          character=owner, job=payload, profile_id=profile_id, accepted_at_ns=time_ns(),
                                                          origin="popup" if popup else "game"))
            except (OSError, ValueError, OperationalError):
                self.cards.finish_fetch_sync()
                raise
        detail = "Counting saved name audio." if action == "check" else None
        self.report_handoff("planning", request, character=owner, detail=detail)
        try:
            job = self.submit_fetch(payload)
        except (OSError, ValueError, OperationalError):
            if action == "fetch" and self.accepted_fetch(path, request):
                self.app.store.set_setting(INFLIGHT, {})
                self.cards.finish_fetch_sync()
            raise
        self.active = job["id"], request, action, path, player["id"], owner, None
        if action == "fetch":
            self.progress_fetch = self.active
        return job["id"]

    def finish(self, job, request, action, *, character=None, stopped=False, pause_reason=None):
        if job["status"] == "failed":
            outcome = {"state": "unavailable", "detail": job.get("error") or "The fetch could not start."}
            self.report_handoff(request=request, character=character, **outcome)
            return outcome
        result = job["result"]
        if action == "check":
            self.remember_coverage(job)
            summary = coverage_summary(result)
            scope = job.get("coverage_scope")
            selected = self.options()
            if scope and (scope[0], scope[1]) != (selected.get("provider_id"), selected.get("coverage")):
                self.report("connected", request, detail="Settings changed. Check coverage for the new selection.",
                            character=character)
                return
            self.report("coverage", request, ready=summary["ready"], total=summary["total"],
                        detail=summary["message"], character=character)
            return
        outcome = {**completed_fetch(result, stopped=stopped, pause_reason=pause_reason), **measured_progress(job)}
        # The desktop result also carries timing and review counts. The WoW
        # snapshot has a separate schema and accepts only these four fields.
        snapshot = {key: outcome[key] for key in ("state", "ready", "total", "detail")}
        self.report_handoff(request=request, character=character, **snapshot)
        return outcome
