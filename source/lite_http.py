"""Loopback-only provider and catalog settings for the standalone WoW companion."""

from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
from shutil import copyfileobj
from tempfile import TemporaryFile
from urllib.parse import parse_qs, urlsplit
from zipfile import BadZipFile

from lite_card_transfer import MAX_BACKUP
from lite_recording_context import ContextUnavailable
from lite_storage_setup import choose_folder, connect_storage
from lite_game_ingress import GameIngress
from lite_name_providers import ENGINES
from lite_name_settings import desktop_preferences
from lite_portable_inputs import SETTING, import_input
from lite_runtime import LiteRuntime
from wow_fetch_desktop import DESKTOP_VERSION, LISTENER_PROTOCOL, client_id, companion_build_id, open_settings


ASSETS = Path(__file__).resolve().with_name("lite_web")
SETTINGS = "/api/spoken/player-names/"
MAX_BODY = 512 * 1024
PROFILE_FIELDS = {"id", "engine", "label", "transport", "model", "endpoint", "has_credentials",
                  "region", "quota_project", "aws_profile", "npc_voices", "ssh_host",
                  "ssh_port", "remote_host", "remote_port", "ssh_key"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    @property
    def app(self):
        return self.server.app

    @property
    def ingress(self):
        return self.server.ingress

    def check_host(self):
        authority = f"127.0.0.1:{self.server.server_port}"
        return self.headers.get("Host") == authority

    def provider_settings(self, profile):
        public = {key: profile[key] for key in PROFILE_FIELDS if key in profile}
        if public.get("has_credentials"):
            secret = self.app.connections.secret(profile["id"])
            public["credential_length"] = len(secret)
        return public

    def respond(self, value, status=200, media_type="application/json; charset=utf-8"):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; media-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        try:
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Switching characters or disabling playback can cancel a pending response.
            self.close_connection = True

    def settings_database_error(self, error):
        code = getattr(error, "sqlite_errorcode", 0) & 0xff
        if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return self.respond({"error_code": "settings_busy",
                                 "error": "The Lite settings database is busy. Try again in a moment."}, 503)
        return self.respond({"error_code": "settings_database_error",
                             "error": "Lite could not access its settings database."}, 500)

    def do_GET(self):
        if not self.check_host():
            return self.respond({"error_code": "loopback_required", "error": "Open this page on 127.0.0.1."}, 403)
        url = urlsplit(self.path)
        try:
            if url.path == "/":
                page = (ASSETS / "index.html").read_bytes().replace(
                    b"__LITE_TOKEN__", self.server.token.encode("ascii"))
                page = page.replace(b"__LITE_SESSION_SCOPE__", getattr(self.server, "session_scope", "").encode("ascii"))
                build = getattr(self.server, "build", None) or ""
                page = page.replace(b"__LITE_BUILD__", build.encode("ascii") if re.fullmatch(r"[a-f0-9]{12,64}", build) else b"")
                page = page.replace(b"__LITE_VERSION__", DESKTOP_VERSION.encode("ascii"))
                if self.ingress.store_managed:
                    page = page.replace(b'<details id="catalog">', b'<details id="catalog" hidden>')
                return self.respond(page, media_type="text/html; charset=utf-8")
            if url.path == "/locale-bundles.js":
                # Language packs contain UI data only. A missing or incomplete optional pack
                # must leave the built-in English interface usable.
                packs = {}
                for path in sorted((ASSETS / "locales").glob("*.json")):
                    if not re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", path.stem):
                        continue
                    try:
                        pack = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=lambda _: None)
                    except (OSError, ValueError):
                        continue
                    if isinstance(pack, dict) and isinstance(pack.get("messages"), dict):
                        packs[path.stem] = pack
                # The external script consumes JSON as data, including keys such as "__proto__".
                data = json.dumps(packs, separators=(",", ":"))
                script = ("window.DMSpeaksLocales = Object.assign(Object.create(null), "
                          "window.DMSpeaksLocales, JSON.parse(" + json.dumps(data) + "));")
                return self.respond(script.encode("ascii"), media_type="text/javascript; charset=utf-8")
            if url.path in ("/lite.js", "/locale.js", "/tooltips.js", "/card-menu.js",
                            "/menu-help-en.js", "/desktop-help.js", "/recording-review.js"):
                return self.respond((ASSETS / url.path[1:]).read_bytes(), media_type="text/javascript; charset=utf-8")
            if url.path == "/lite.css":
                return self.respond((ASSETS / "lite.css").read_bytes(), media_type="text/css; charset=utf-8")
            if url.path in ("/alliance.png", "/horde.png", "/unknown.png", "/dmspeaks-mark.png"):
                return self.respond((ASSETS / url.path[1:]).read_bytes(), media_type="image/png")
            if re.fullmatch(r"/blizzard-avatars/(?:0-0|[1-8]-[01])\.jpg", url.path):
                portrait_file = ASSETS / url.path[1:]
                if portrait_file.is_file():
                    return self.respond(portrait_file.read_bytes(), media_type="image/jpeg")
                return self.respond((ASSETS / "unknown.png").read_bytes(), media_type="image/png")
            portrait = re.fullmatch(r"/character-portraits/(us|eu|kr|tw)/([0-9a-f]{20})\.jpg", url.path)
            if portrait:
                image = self.ingress.character_portrait(portrait[2], portrait[1])
                if image:
                    return self.respond(image, media_type="image/jpeg")
                return self.respond({"error": "Character portrait unavailable."}, 404)
            if url.path == "/api/wow-fetch-ready":
                query = parse_qs(url.query)
                if query.get("client") != [client_id(self.ingress.root)]:
                    return self.respond({"ready": False}, 404)
                result = {"ready": True, "protocol": LISTENER_PROTOCOL,
                          "catalog": self.server.catalog, "build": self.server.build}
                if "data" in query:
                    result["data_matches"] = query["data"] == [client_id(self.app.folder)]
                return self.respond(result)
            if url.path == SETTINGS + "game-options":
                character, options = self.ingress.popup_character(), self.ingress.options()
                return self.respond({**options, "preparation": self.ingress.preparation_state(),
                                     "last_coverage": self.ingress.last_coverage(character, options),
                                     "game_character": character})
            if url.path == SETTINGS + "characters":
                return self.respond(self.ingress.character_profiles())
            if url.path == SETTINGS + "profile-archive":
                return self.respond(self.ingress.removed_profiles())
            if url.path == SETTINGS + "inputs":
                records = self.app.store.setting(SETTING, {})
                return self.respond({kind: {key: item[key] for key in ("count", "sha256") if key in item}
                                     for kind, item in records.items()})
            if url.path == SETTINGS + "card-sync":
                return self.respond(self.ingress.cards.sync_state())
            if url.path == SETTINGS + "preferences":
                return self.respond(desktop_preferences(self.app.store))
            if url.path == SETTINGS + "setup":
                profiles = [self.provider_settings(profile) for profile in self.app.connections.profiles()
                            if profile.get("transport") in {"ssh", "cloud"}]
                return self.respond({"engines": ENGINES, "profiles": profiles,
                                     "preferences": desktop_preferences(self.app.store),
                                     "options": self.ingress.options(),
                                     "game_character": self.ingress.popup_character()})
        except sqlite3.OperationalError as error:
            return self.settings_database_error(error)
        except (OSError, ValueError) as error:
            return self.respond({"error": str(error)}, 400)
        return self.respond({"error": "Not found."}, 404)

    def download_cards(self, selected):
        with TemporaryFile() as stream:
            self.ingress.cards.export_zip(stream, selected)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="DMSpeaks-name-cards.zip"')
            self.send_header("Content-Length", str(stream.tell()))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            stream.seek(0)
            copyfileobj(stream, self.wfile, 1024 * 1024)

    def upload_cards(self):
        if self.headers.get_content_type() != "application/zip":
            return self.respond({"error_code": "card_zip_required", "error": "Choose a name-card ZIP file."}, 415)
        remaining = int(self.headers.get("Content-Length", "0"))
        if not 0 < remaining <= MAX_BACKUP:
            return self.respond({"error_code": "card_size_limit", "error": "Choose a name-card backup under 2 GiB."}, 413)
        with TemporaryFile() as stream:
            while remaining:
                block = self.rfile.read(min(remaining, 1024 * 1024))
                if not block:
                    raise ValueError("The upload stopped before the backup was complete.")
                stream.write(block)
                remaining -= len(block)
            stream.seek(0)
            return self.respond(self.ingress.cards.import_zip(stream))

    def do_POST(self):
        origin = self.headers.get("Origin")
        if not self.check_host() or (origin and origin != f"http://127.0.0.1:{self.server.server_port}"):
            return self.respond({"error_code": "loopback_required", "error": "Open this page on 127.0.0.1."}, 403)
        if self.headers.get("X-Lite-Token") != self.server.token:
            return self.respond({"error_code": "settings_session_expired", "error": "Refresh this Lite tab and try again."}, 403)
        try:
            if urlsplit(self.path).path == SETTINGS + "card-import":
                return self.upload_cards()
            if self.headers.get_content_type() != "application/json":
                return self.respond({"error_code": "settings_json_required", "error": "Send a JSON settings object."}, 415)
            count = int(self.headers.get("Content-Length", "0"))
            if not 0 < count <= MAX_BODY:
                return self.respond({"error_code": "settings_size_limit", "error": "Settings request is too large or empty."}, 413)
            data = json.loads(self.rfile.read(count))
            if not isinstance(data, dict):
                raise ValueError("Supply a settings object.")
            path = urlsplit(self.path).path
            if path == SETTINGS + "card-export":
                return self.download_cards(data.get("character_id"))
            if path == SETTINGS + "card-export-folder":
                folder = data.get("folder")
                backup = self.ingress.cards.save_backup(folder, data.get("character_id"))
                return self.respond({"path": str(backup)})
            if path == SETTINGS + "recordings":
                return self.respond(self.ingress.review_recordings(data))
            if path == SETTINGS + "recording-audio":
                if data.get("explicit_play") is True:
                    content, media_type = self.ingress.play_recording(data)
                    return self.respond(content, media_type=media_type)
                return self.respond(self.ingress.review_audio(data), media_type="audio/ogg")
            if path == SETTINGS + "recording-context-audio":
                return self.respond(self.ingress.review_context_audio(data), media_type="audio/wav")
            if path == SETTINGS + "profile-archive":
                return self.respond(self.ingress.change_profile_removal(data))
            if path == SETTINGS + "account-label":
                return self.respond(self.ingress.rename_account(data))
            if path == SETTINGS + "card-folder":
                return self.respond(choose_folder())
            if path == SETTINGS + "card-connect":
                return self.respond(connect_storage(data.get("medium"), data.get("address")))
            if path == SETTINGS + "card-sync":
                if "folder" in data:
                    return self.respond(self.ingress.cards.configure_sync(data["folder"], data.get("medium", "shared"), data.get("lan_only"),
                                                                        data.get("nas", False), data.get("via_tailscale", False),
                                                                        data.get("cache_offline", False)))
                return self.respond(self.ingress.cards.synchronize())
            if path == SETTINGS + "provider":
                return self.respond(self.provider_settings(self.ingress.save_provider(data)))
            if path == SETTINGS + "provider-remove":
                return self.respond(self.ingress.remove_provider(data.get("provider_id")))
            if path == SETTINGS + "provider-check":
                return self.respond(self.ingress.check_provider(data.get("provider_id")))
            if path == SETTINGS + "coverage":
                result = (self.ingress.coverage_status(data["job_id"])
                          if "job_id" in data else self.ingress.start_coverage(data))
                return self.respond(result)
            if path == SETTINGS + "fetch":
                return self.respond(self.ingress.popup_fetch(data))
            if path == SETTINGS + "game-options":
                if data.get("provider_id") == "fish-local":
                    raise ValueError('Choose Fish Audio or a self-hosted Fish server over SSH for this Lite installation.')
                with self.ingress.request_lock:
                    return self.respond(self.ingress.options(data))
            if path == SETTINGS + "preferences":
                return self.respond(desktop_preferences(self.app.store, data))
            if path == SETTINGS + "inputs":
                if self.ingress.store_managed:
                    raise ValueError("Update the DMSpeaks add-on in CurseForge to change dialogue files.")
                if data.get("kind") == "plans":
                    return self.respond(self.ingress.replace_catalog(data, importer=import_input))
                if data.get("kind") == "playback":
                    return self.respond(self.ingress.import_playback(data))
                if self.ingress.preparation_state()["state"] == "preparing":
                    raise ValueError("Wait for dialogue verification before importing saved returns.")
                return self.respond(import_input(self.app, data))
        except sqlite3.OperationalError as error:
            return self.settings_database_error(error)
        except ContextUnavailable as error:
            return self.respond({"error_code": "recording_context_unavailable", "error": str(error)}, 422)
        except (OSError, ValueError, BadZipFile) as error:
            return self.respond({"error": str(error)}, 400)
        return self.respond({"error": "Not found."}, 404)


def serve(*, port, folder, on_ready, connection_file, wow_root, game_plans, game_plans_sha256,
          game_paced, store_managed=False):
    with ExitStack() as cleanup:
        server = cleanup.enter_context(ThreadingHTTPServer(("127.0.0.1", port), Handler, bind_and_activate=False))
        # Windows can refuse an asset connection during the browser's initial burst of parallel GETs.
        server.request_queue_size = 64
        server.server_bind()
        server.server_activate()
        server.app = LiteRuntime(folder, connection_file)
        server.app.store_managed = store_managed
        cleanup.callback(server.app.close)
        server.token = secrets.token_urlsafe(32)
        server.session_scope = client_id(wow_root) + "." + client_id(folder)
        server.catalog = game_plans_sha256
        server.build = companion_build_id()

        def open_provider(provider):
            open_settings(server.server_port, provider=provider)
            return True

        server.ingress = GameIngress(server.app, wow_root, open_provider,
                                     settings_port=server.server_port, build_id=server.build,
                                     store_managed=store_managed)
        server.ingress.start(game_plans, game_plans_sha256, game_paced)
        cleanup.callback(server.ingress.close)
        if os.name == "nt" and store_managed:
            from lite_windows_progress import start

            close_hud = start(wow_root, server.ingress.progress_display)
            if close_hud is not None:
                server.app.live_hud = server.ingress.live_hud = close_hud.is_alive
                cleanup.callback(close_hud)
        print(f"DMSpeaks Lite: http://127.0.0.1:{server.server_port}", flush=True)
        if on_ready:
            on_ready(server.server_port)
        server.serve_forever()
