"""Opt-in pickup of one character's SavedVariables by the desktop companion."""

from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic

from lite_name_bridge import imported_character, load_settings
from lite_name_settings import DEFAULTS, PREFS


SETTING = "lite_automatic_names"


def addons_folder(value):
    path = Path(value).expanduser() if isinstance(value, str) else None
    if (path is None or not path.is_absolute() or path.name.casefold() != "addons"
            or not (path / "Spoken/Spoken.toc").is_file()
            or not (path / "Spoken_Quests/Spoken_Quests.toc").is_file()):
        raise ValueError("Choose the AddOns folder containing Spoken and Spoken Quests.")
    return path


class AutomaticNames:
    def __init__(self, app):
        self.app = app
        self.lock = Lock()
        self.closed = Event()
        self.wake = Event()
        self.thread = None
        self.last_input = None
        self.error = None
        self.job_id = None
        self.local_retry_at = None
        self.local_retries = 0

    def start(self):
        self.thread = Thread(target=self.watch, name="lite-auto-pickup", daemon=True)
        self.thread.start()

    def close(self):
        self.closed.set()
        self.wake.set()
        if self.thread is not None:
            self.thread.join()

    def configure(self, data):
        enabled = data.get("enabled")
        if type(enabled) is not bool:
            raise ValueError("Choose whether automatic fetching is on or off.")
        config = {"enabled": enabled}
        if enabled:
            key = data.get("character_id")
            if not any(row["id"] == key for row in self.app.store.setting("characters", [])):
                raise ValueError("Select a saved character.")
            path = data.get("settings_path")
            if not isinstance(path, str):
                raise ValueError("Choose the saved WoW settings file.")
            saved = load_settings(path)
            reported = imported_character(saved)
            if reported and reported["id"] != key:
                raise ValueError("Import and select the character currently saved by WoW.")
            cached = data.get("cached_only", False)
            metered = data.get("allow_metered", False)
            coverage = data.get("coverage", "route")
            if type(cached) is not bool or type(metered) is not bool:
                raise ValueError("Choose valid automatic fetch options.")
            if coverage not in {"starting-area", "route", "all"}:
                raise ValueError("Choose valid automatic fetch coverage.")
            config.update(character_id=key, settings_path=str(Path(path).expanduser()),
                          addons_folder=str(addons_folder(data.get("addons_folder"))),
                          coverage=coverage, cached_only=cached,
                          approved_provider=saved["provider_id"] if metered else None)
        with self.lock:
            if self.job_id:
                existing = self.app.job(self.job_id)
                if existing["status"] in {"queued", "running"}:
                    if enabled:
                        raise ValueError("Pause the automatic fetch before changing its settings.")
                    self.app.pause_player_name_fetch(self.job_id)
            self.app.store.set_setting(SETTING, config)
            self.last_input = None
            self.error = None
            self.local_retry_at = None
            self.local_retries = 0
            if enabled:
                self.job_id = None
        self.wake.set()
        return self.state()

    def provider_changed(self, provider_id):
        with self.lock:
            config = self.app.store.setting(SETTING, {"enabled": False})
            if not config["enabled"]:
                return
            try:
                saved = load_settings(config["settings_path"])
            except (OSError, ValueError):
                return
            if saved["provider_id"] != provider_id:
                return
            self.last_input = None
        self.wake.set()

    def state(self):
        with self.lock:
            config = self.app.store.setting(SETTING, {"enabled": False})
            return dict(config=config, error=self.error,
                        job=self.app.job(self.job_id) if self.job_id else None)

    def watch(self):
        while not self.closed.is_set():
            with self.lock:
                try:
                    self.tick()
                except (OSError, ValueError) as error:
                    self.error = str(error)
            self.wake.wait(2)
            self.wake.clear()

    def tick(self):
        config = self.app.store.setting(SETTING, {"enabled": False})
        if not config["enabled"]:
            return
        previous = self.app.job(self.job_id) if self.job_id else None
        if previous and previous["status"] in {"queued", "running"}:
            return
        saved = load_settings(config["settings_path"])
        reported = imported_character(saved)
        if reported and reported["id"] != config["character_id"]:
            raise ValueError("WoW saved a different character. Import and select that character to fetch its clips.")
        saved.pop("character", None)
        level = saved.pop("character_level", None)
        request = saved.pop("fetch_request", None)
        action = saved.pop("request_action", "fetch")
        if action != "fetch":
            return
        requested = saved.pop("request_provider", None)
        if requested is not None and requested != saved["provider_id"]:
            raise ValueError("Open Fetch Settings to save the provider chosen in WoW.")
        from lite_portable_inputs import SETTING as INPUTS
        imported = self.app.store.setting(INPUTS, {})
        signature = (tuple(sorted(saved.items())), tuple(sorted((kind, entry["sha256"])
                                                                for kind, entry in imported.items())), request, level)
        retry = signature == self.last_input
        if retry:
            result = (previous or {}).get("result") or {}
            aligner_busy = any(item.get("reason") == "The shared phoneme aligner is occupied."
                               for item in result.get("statuses", []))
            if not aligner_busy or result.get("paused") or self.local_retries >= 3:
                return
            if self.local_retry_at is None:
                self.local_retry_at = monotonic() + 10 * 2 ** self.local_retries
                return
            if monotonic() < self.local_retry_at:
                return
            self.local_retries += 1
        else:
            self.local_retries = 0
        self.local_retry_at = None
        addons_folder(config["addons_folder"])
        prefs = {**self.app.store.setting(PREFS, DEFAULTS), **saved}
        self.app.store.set_setting(PREFS, prefs)
        job = self.app.submit("spoken_player_names", {
            **prefs, "character_id": config["character_id"], "coverage": saved.get("coverage", config["coverage"]),
            "addons_folder": config["addons_folder"], "submit": True, "automatic": True,
            "cached_only": config["cached_only"] or retry,
            "allow_metered": config["approved_provider"] == saved["provider_id"],
            **({"reported_player_level": level} if level is not None else {}),
        })
        self.last_input = signature
        self.error = None
        self.job_id = job["id"]
