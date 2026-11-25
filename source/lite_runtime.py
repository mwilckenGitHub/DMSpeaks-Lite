"""State, speech providers, and the name-fetch queue for the WoW companion."""

from pathlib import Path
from threading import Lock

from activity_log import ActivityLog
from connections import Connections
from name_fetch_jobs import NameFetchJobs, OPERATION
from providers import Providers
from settings_store import SettingsStore


class LiteRuntime:
    def __init__(self, folder, connection_file):
        self.folder = Path(folder).resolve()
        self.store = SettingsStore(self.folder)
        try:
            self.activity = ActivityLog(self.folder / "logs")
            self.connections = Connections(connection_file, self.folder)
            try:
                self.providers = Providers(self.connections, self.folder / "renders", self.folder)
            except BaseException:
                self.connections.close()
                raise
        except BaseException:
            self.store.close()
            raise
        self.jobs = {}
        self.lock = Lock()
        self.name_fetch = NameFetchJobs(self.jobs, self.lock, self.activity)

    def submit(self, operation, data):
        if operation != OPERATION:
            raise ValueError("This companion handles player-name fetches.")
        from spoken_player_names import run

        def fetch(request, report, **callbacks):
            return run(self, request, report, **callbacks)

        return self.name_fetch.submit(data, fetch)

    def job(self, ident):
        with self.lock:
            if ident not in self.jobs:
                raise ValueError("Unknown job.")
            return dict(self.jobs[ident])

    def pause_player_name_fetch(self, ident):
        return self.name_fetch.pause(ident)

    def close(self):
        self.name_fetch.close()
        try:
            self.connections.close()
        finally:
            self.store.close()
