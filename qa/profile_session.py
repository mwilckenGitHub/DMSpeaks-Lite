"""Exercise profile removal with a restarted loopback listener and disposable settings."""
from contextlib import contextmanager
from html.parser import HTMLParser
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))
from lite_game_ingress import GameIngress
from lite_http import Handler, SETTINGS
from settings_store import SettingsStore
from wow_fetch_desktop import client_id


class Meta(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.values = {}
        self.feed(document.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and "content" in attrs:
            self.values[attrs.get("name")] = attrs["content"]


@contextmanager
def listener(ingress, folder, port=0):
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        server.ingress = ingress
        server.app = SimpleNamespace(folder=folder)
        server.token = secrets.token_urlsafe(32)
        server.session_scope = client_id(ingress.root) + "." + client_id(folder)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            thread.join()


def request(port, path, *, data=None, headers=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        body = json.dumps(data) if data is not None else None
        connection.request("GET" if data is None else "POST", path, body, headers or {})
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class ProfileRestart(unittest.TestCase):
    def test_restart_and_restore(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            recording = root / "game/Interface/AddOns/DMSpeaksLiteStatus/Names/alice.ogg"
            recording.parent.mkdir(parents=True)
            recording.write_bytes(b"fixture recording")
            store = SettingsStore(root / "user")
            try:
                game = GameIngress.__new__(GameIngress)
                game.root = root / "game"
                game.store_managed = True
                game.request_lock, game.profile_lock = Lock(), Lock()
                game.app, game.active = SimpleNamespace(store=store), None
                game.name_recording_counts = lambda: {"alice": 1}
                profile = {"name": "Alice", "realm": "Test", "race": "Human", "class": "Mage"}
                key = "lite_wow_profiles_" + str(game.root)
                store.set_setting(key, {"alice": profile})
                with listener(game, root / "user") as port:
                    original = Meta(request(port, "/")[1]).values

                with listener(game, root / "user", port) as same:
                    current = Meta(request(same, "/")[1]).values
                    self.assertNotEqual(original["lite-token"], current["lite-token"])
                    self.assertTrue(current["lite-session-scope"])
                    self.assertEqual(original["lite-session-scope"], current["lite-session-scope"])
                    route = SETTINGS + "profile-archive"
                    remove = {"character_id": "alice", "removed": True}
                    headers = {"X-Lite-Token": current["lite-token"], "Origin": f"http://127.0.0.1:{port}",
                               "Content-Type": "application/json"}
                    for unauthorised in ({"Origin": "http://example.invalid"}, {"Host": "example.invalid"}):
                        status, body = request(port, route, data=remove, headers={**headers, **unauthorised})
                        self.assertEqual(status, 403)
                        self.assertNotEqual(json.loads(body)["error_code"], "settings_session_expired")
                    status, body = request(port, route, data=remove,
                                           headers={**headers, "X-Lite-Token": original["lite-token"]})
                    self.assertEqual((status, json.loads(body)["error_code"]), (403, "settings_session_expired"))
                    self.assertEqual(game.removed_profiles(), {})
                    self.assertEqual(request(port, route, data=remove, headers=headers)[0], 200)
                    self.assertIn("alice", game.removed_profiles())
                    self.assertEqual(store.setting(key), {"alice": profile})
                    self.assertEqual(request(port, route, data={**remove, "removed": False}, headers=headers)[0], 200)
                    self.assertEqual(game.removed_profiles(), {})
                    self.assertEqual(recording.read_bytes(), b"fixture recording")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
