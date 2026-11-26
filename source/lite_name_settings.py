"""Local companion settings, with credentials handled by the connection store."""
import re
from uuid import uuid4

from lite_name_providers import ENGINES, ENDPOINTS, FREE_MODEL, NameProvider

PREFS = "lite_player_name_preferences"
# Separate from the fetch worker's preference record, which is updated on game events.
RECORDING_PLAYBACK = "lite_recording_playback_enabled"
DEFAULTS = dict(provider_id=None, progress="bar", keep_raw_wav=False,
                wav_storage_notice_seen=False, character_id=None, coverage="starting-area")


def preferences(data, previous=None):
    if not isinstance(data, dict):
        raise ValueError("Choose valid player-name settings.")
    values = {**DEFAULTS, **(previous or {})}
    values.pop("stop_on_dialogue_error", None)
    for key in DEFAULTS:
        if key in data:
            values[key] = data[key]
    if values["provider_id"] is not None and (not isinstance(values["provider_id"], str) or not values["provider_id"]):
        raise ValueError("Choose a configured speech provider.")
    if values["progress"] not in ("bar", "compact", "off"):
        raise ValueError("Choose a progress display.")
    if type(values["keep_raw_wav"]) is not bool:
        raise ValueError("Raw WAV storage must be explicitly on or off.")
    if type(values["wav_storage_notice_seen"]) is not bool:
        raise ValueError("The WAV storage notice choice is invalid.")
    if values["character_id"] is not None and not isinstance(values["character_id"], str):
        raise ValueError("Choose a saved character.")
    if values["coverage"] not in ("starting-area", "route", "all"):
        raise ValueError("Choose valid dialogue coverage.")
    return values


def desktop_preferences(store, changes=None):
    """Read or update the local companion's display and recording review options."""
    with store.lock:
        saved = preferences({}, store.setting(PREFS))
        if changes is not None:
            playback = changes.get("recording_playback_enabled", False)
            if type(playback) is not bool:
                raise ValueError("Choose whether to enable recording playback.")
            display = {key: changes[key] for key in ("keep_raw_wav", "wav_storage_notice_seen", "progress")
                       if key in changes}
            if display:
                saved = preferences(display, saved)
                store.set_setting(PREFS, saved)
            if "recording_playback_enabled" in changes:
                store.set_setting(RECORDING_PLAYBACK, playback)
        return {**saved, "recording_playback_enabled": store.setting(RECORDING_PLAYBACK) is True}


def personal_connection_id(ident):
    return isinstance(ident, str) and (
        ident == "lite-fish_omni" or re.fullmatch(r"lite-local-[0-9a-f]{32}", ident) is not None)


def configure(connections, data):
    if not isinstance(data, dict) or data.get("engine") not in ENGINES:
        raise ValueError("Choose a supported speech provider.")
    engine = data["engine"]
    ident = "fish-s21-free" if engine == "fish" else "lite-" + engine
    label = ENGINES[engine]
    if engine == "fish_omni":
        selected = data.get("id")
        if selected is not None:
            if not personal_connection_id(selected) or connections.profile(selected).get("transport") != "ssh":
                raise ValueError("Choose a saved personal SSH connection.")
            ident = selected
        else:
            ident = "lite-local-" + uuid4().hex
        label = data.get("label", ENGINES[engine])
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60 or not label.isprintable():
            raise ValueError("Enter a connection name of up to 60 characters.")
        label = label.strip()
    settings = dict(engine=engine, transport="ssh" if engine=="fish_omni" else "cloud", label=label,
                    endpoint=ENDPOINTS.get(engine, ""), language="en-US")
    for field in ("region", "model", "quota_project", "aws_profile", "npc_voices", "ssh_host", "ssh_port",
                  "remote_host", "remote_port", "ssh_key"):
        value = data.get(field, "")
        if not isinstance(value, str) or len(value) > 200_000 or "\x00" in value:
            raise ValueError("Enter valid provider settings.")
        settings[field] = value.strip()
    if engine == "fish":
        settings.update(model=FREE_MODEL, capabilities="references,speed,directions", syntax="brackets")
    if engine == "fish_omni":
        settings.update(model="drama-3", capabilities="references,seed,timestamps", syntax="brackets")
    if engine == "azure":
        settings["model"] = "azure-speech"
    if engine == "google":
        settings["model"] = "google-cloud-tts"
    secret = data.get("api_key")
    if secret is not None:
        if not isinstance(secret, str) or len(secret) > 8192 or not secret.strip().isprintable():
            raise ValueError("Paste only the provider token or key.")
        secret = secret.strip() or None

    class Candidate:
        def profile(self, _):
            return settings

    NameProvider(Candidate(), ident, submit=False, allow_metered=False, timestamps=True)
    return connections.save(ident, settings, secret)



def test_connection(app, ident):
    """Check the selected provider without submitting speech or references."""
    from urllib.parse import urlsplit
    from urllib.request import Request
    from fish_community_tts import open_loopback
    import json

    provider = NameProvider(app.connections, ident, submit=False, allow_metered=False, timestamps=True)
    if provider.shared:
        return dict(provider.shared.check(), provider_id=ident)
    if provider.engine != "fish_omni":
        profile = {key: value for key, value in provider.profile.items() if key != "check_path"}
        return dict(app.providers.check(ident, profile=profile), provider_id=ident)
    provider.verify_ssh_target()
    endpoint = app.connections.endpoint(provider.profile)
    address = urlsplit(endpoint)
    if (address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port
            or address.username or address.password or address.path or address.query or address.fragment):
        raise ValueError("The SSH service needs a verified loopback forward.")
    try:
        request = Request(endpoint + "/health", headers={"Accept": "application/json"})
        with open_loopback(request, timeout=10) as response:
            data = json.loads(response.read(4096))
        if not isinstance(data, dict) or data.get("status") != "healthy":
            raise ValueError("The SSH service did not report a healthy Fish runtime.")
    except (OSError, json.JSONDecodeError):
        raise ValueError("OpenSSH connected, but the Fish health check failed.") from None
    return dict(provider_id=ident, ok=True, speech_generated=False,
                message="Trusted SSH tunnel and Fish health check passed. No speech was requested.")
