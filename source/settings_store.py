"""SQLite settings shared by the editor and the standalone companion."""

import json
from pathlib import Path
import sqlite3
from threading import RLock


def read_setting(database, key, default=None):
    row = database.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def write_setting(database, key, value):
    with database:
        database.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))


class SettingsStore:
    def __init__(self, folder):
        Path(folder).mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        # Preserve settings in existing companion installations.
        self.db = sqlite3.connect(Path(folder) / "studio.sqlite", check_same_thread=False)
        try:
            self.db.execute("PRAGMA journal_mode = WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        except BaseException:
            self.db.close()
            raise

    def setting(self, key, default=None):
        with self.lock:
            return read_setting(self.db, key, default)

    def set_setting(self, key, value):
        with self.lock:
            write_setting(self.db, key, value)

    def close(self):
        with self.lock:
            self.db.close()
