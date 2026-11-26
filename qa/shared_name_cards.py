"""Exercise two independent Lite installations and a disposable shared folder."""

from contextlib import ExitStack
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import numpy as np
import soundfile as sf

from lite_game_ingress import GameIngress
from lite_share_playback import LIBRARY
from lite_store_playback import name_audio_relative, playable_saved_names, write_name_index
from roster import import_roster
from settings_store import SettingsStore


class SharedNameCards(unittest.TestCase):
    def test_recording_survives_delayed_delivery_and_disconnect(self):
        with TemporaryDirectory() as temporary, ExitStack() as scope:
            root = Path(temporary)
            share = root / "Shared"
            share.mkdir()

            def installation(name):
                folder = root / name
                addons = folder / "game" / "Interface" / "AddOns"
                for addon in ("DMSpeaksLite", "Spoken", "Spoken_Quests"):
                    (addons / addon).mkdir(parents=True)
                    (addons / addon / (addon + ".toc")).write_text(f"## Title: {addon}", encoding="utf-8")
                settings = folder / "settings"
                settings.mkdir()
                store = SettingsStore(settings)
                scope.callback(store.close)
                return GameIngress(SimpleNamespace(folder=settings, store=store), folder / "game", None)

            sender, recipient = installation("Sender"), installation("Recipient")
            profile = {"name": "Rowan", "realm": "Fixture", "region": "us", "client": "Forever",
                       "class": "Priest", "race": "Human", "gender": "Female"}
            ident = import_roster([profile])[0]["id"]
            sender.app.store.set_setting(sender.cards.scope, {ident: profile})
            stream = BytesIO()
            sf.write(stream, np.sin(np.arange(3000) * .07) * .25, 44100, format="OGG", subtype="VORBIS")
            body = stream.getvalue()
            checksum = sha256(body).hexdigest()
            clip = {"sha256": checksum, "samples": 3000, "caption": "Rowan", "starts": [0]}
            index = {"a" * 64 + ":1": {"rowan": clip}}
            sender_path = sender.addons / name_audio_relative(clip)
            sender_path.parent.mkdir(parents=True)
            sender_path.write_bytes(body)
            write_name_index(sender.addons, index)

            sender.cards.configure_sync(str(share), "googledrive", cache_offline=True)
            recipient.cards.configure_sync(str(share), "googledrive", cache_offline=True)
            self.assertEqual(sender.cards.synchronize()["state"], "synced")
            self.assertEqual(sender.cards.storage_states({ident: profile})[ident]["state"], "pending")

            # A cloud client can make the index visible before the audio arrives.
            object_path = share / LIBRARY / "audio" / (checksum + ".ogg")
            delayed = object_path.with_suffix(".delayed")
            object_path.rename(delayed)
            delayed_state = recipient.cards.synchronize()
            self.assertEqual(delayed_state["state"], "waiting")
            self.assertIn("Waiting for recordings", delayed_state["error"])
            self.assertEqual(playable_saved_names(recipient.addons), {})
            self.assertEqual(recipient.app.store.setting(recipient.cards.scope, {}), {})
            delayed.rename(object_path)

            self.assertEqual(recipient.cards.synchronize()["added"], 1)
            self.assertEqual(recipient.cards.synchronize()["added"], 0)
            self.assertEqual(playable_saved_names(recipient.addons), index)
            self.assertEqual(recipient.app.store.setting(recipient.cards.scope)[ident], profile)
            self.assertEqual((recipient.addons / name_audio_relative(clip)).read_bytes(), body)
            self.assertEqual(sender.cards.synchronize()["state"], "synced")
            self.assertEqual(sender.cards.storage_states({ident: profile})[ident]["state"], "synced")

            # A cold scan sees the bad file even though both devices own the good take.
            object_path.write_bytes(body + b"changed")
            sender = GameIngress(sender.app, sender.root, None)
            recipient = GameIngress(recipient.app, recipient.root, None)
            self.assertEqual(sender.cards.synchronize()["state"], "synced")
            self.assertEqual(object_path.read_bytes(), body)
            self.assertEqual(recipient.cards.synchronize()["added"], 0)

            # A temporarily missing remote object can be repaired from either device.
            object_path.unlink()
            recipient = GameIngress(recipient.app, recipient.root, None)
            self.assertEqual(recipient.cards.synchronize()["state"], "synced")
            self.assertEqual(object_path.read_bytes(), body)

            self.assertEqual(recipient.cards.configure_sync("")["state"], "off")
            self.assertEqual(playable_saved_names(recipient.addons), index)
