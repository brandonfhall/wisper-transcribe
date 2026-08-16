"""Tests for /record and /recordings HTML routes + /api/record JSON API."""
from __future__ import annotations

import asyncio
import json
import wave
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path):
    """TestClient with server.json writing patched to tmp_path."""
    import wisper_transcribe.web.app as app_module
    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path), \
         patch("wisper_transcribe.web.routes.record.get_data_dir", return_value=tmp_path):
        from wisper_transcribe.web.app import create_app
        test_app = create_app()
        with TestClient(test_app) as c:
            yield c, tmp_path


@pytest.fixture(autouse=True)
def _no_live_transcription(monkeypatch):
    """Starting a local session (via the route) submits a real Phase 2
    JOB_LIVE job. The `client` fixture's app runs a REAL JobQueue background
    worker (started by app lifespan), which would pick that job up and try
    to load an actual Whisper model -- exactly what CLAUDE.md's "no GPU, no
    network, no real audio in tests" rule forbids, and it hangs the test
    besides. Every test in this file gets JOB_LIVE submission suppressed by
    default; tests/test_record_live_routes.py exercises the live-job wiring
    itself with `queue.submit_live`/`run_live_loop` explicitly mocked.
    """
    monkeypatch.setattr(
        "wisper_transcribe.web.routes.record._start_live_transcription", lambda *a, **kw: None
    )


def test_record_start_creates_recording(client):
    c, _ = client
    resp = c.post("/api/record/start", json={"voice_channel_id": "123", "guild_id": "G1"})
    assert resp.status_code == 201
    data = resp.json()
    assert "recording_id" in data or "id" in data
    assert data.get("status") == "recording"


def test_record_start_missing_voice_channel_returns_400(client):
    c, _ = client
    resp = c.post("/api/record/start", json={})
    assert resp.status_code == 400


def test_record_stop_with_no_active_session_returns_400(client):
    c, _ = client
    resp = c.post("/api/record/stop")
    assert resp.status_code == 400


def test_record_status_idle_when_no_active_session(client):
    c, _ = client
    resp = c.get("/api/record/status")
    assert resp.status_code == 200
    assert resp.json() == {"active": False}


def test_record_status_reports_active_local_session(client):
    """Powers the global recording-status banner (base.html + app.js) --
    must reflect a session in progress on any page, not just /record."""
    c, data_dir = client
    fake_result = {
        "microphones": [{"id": "mic1", "name": "Mic"}],
        "loopbacks": [{"id": "loop1", "name": "Loop"}],
        "available": True,
    }
    mgr = _scripted_local_capture_manager(data_dir)
    c.app.state.local_capture_manager = mgr
    try:
        with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
            start = c.post("/api/record/start-local", json={"mic_id": "mic1", "system_id": "loop1"})
        rec_id = start.json()["id"]

        resp = c.get("/api/record/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is True
        assert data["id"] == rec_id
        assert data["source"] == "local"
        assert data["status"] == "recording"
    finally:
        mgr.stop_session()


def test_channels_no_token_returns_no_token_error(client):
    """GET /api/record/channels returns {error: no_token} when no bot token is set."""
    c, _ = client
    from unittest.mock import patch
    import os
    with patch("wisper_transcribe.web.routes.record.load_config", return_value={}), \
         patch.dict(os.environ, {}, clear=False) as env:
        env.pop("DISCORD_BOT_TOKEN", None)
        resp = c.get("/api/record/channels")
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] == "no_token"
    assert data["guilds"] == []


def test_channels_returns_voice_channels(client):
    """GET /api/record/channels returns guilds with voice channels only."""
    c, _ = client
    from unittest.mock import AsyncMock, MagicMock, patch
    import os

    guilds_resp = MagicMock()
    guilds_resp.status_code = 200
    guilds_resp.json.return_value = [{"id": "111111111111111111", "name": "D&D Server"}]
    guilds_resp.raise_for_status = MagicMock()

    channels_resp = MagicMock()
    channels_resp.status_code = 200
    channels_resp.json.return_value = [
        {"id": "222222222222222222", "name": "Voice General", "type": 2},
        {"id": "333333333333333333", "name": "#text-channel",  "type": 0},
    ]

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=[guilds_resp, channels_resp])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("wisper_transcribe.web.routes.record.load_config",
               return_value={"discord_bot_token": "Bot.token"}), \
         patch.dict(os.environ, {}, clear=False) as env, \
         patch("httpx.AsyncClient", return_value=mock_client):
        env.pop("DISCORD_BOT_TOKEN", None)
        resp = c.get("/api/record/channels")

    assert resp.status_code == 200
    data = resp.json()
    assert "guilds" in data
    assert len(data["guilds"]) == 1
    assert data["guilds"][0]["name"] == "D&D Server"
    voice = data["guilds"][0]["voice_channels"]
    assert len(voice) == 1          # text channel filtered out
    assert voice[0]["name"] == "Voice General"


def test_channels_invalid_token_returns_error(client):
    """GET /api/record/channels returns {error: invalid_token} on 401 from Discord."""
    c, _ = client
    from unittest.mock import AsyncMock, MagicMock, patch
    import os

    mock_resp = MagicMock()
    mock_resp.status_code = 401

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("wisper_transcribe.web.routes.record.load_config",
               return_value={"discord_bot_token": "bad-token"}), \
         patch.dict(os.environ, {}, clear=False) as env, \
         patch("httpx.AsyncClient", return_value=mock_client):
        env.pop("DISCORD_BOT_TOKEN", None)
        resp = c.get("/api/record/channels")

    assert resp.status_code == 200
    assert resp.json()["error"] == "invalid_token"
    assert resp.json()["guilds"] == []


# ---------------------------------------------------------------------------
# Live local recording Phase 1 — device enumeration, start/stop-local,
# cross-manager (Discord vs local) mutual exclusion
# ---------------------------------------------------------------------------

def _scripted_local_capture_manager(tmp_path, n_ticks=0):
    """A LocalCaptureManager wired with a scripted, finite (no real audio
    devices, no soundcard import) capture_factory + instant ticker, for
    swapping onto `app.state.local_capture_manager` in tests."""
    from wisper_transcribe.web.local_capture import LocalCaptureManager

    def capture_factory(device_id, samplerate):
        return iter(())  # no blocks -- fine for lifecycle/route tests

    def ticker():
        return iter([None] * n_ticks)

    return LocalCaptureManager(data_dir=tmp_path, capture_factory=capture_factory, ticker=ticker)


def test_devices_endpoint_unavailable_when_soundcard_not_installed(client):
    import sys
    c, _ = client
    with patch.dict(sys.modules, {"soundcard": None}):
        resp = c.get("/api/record/devices")
    assert resp.status_code == 200
    assert resp.json() == {"microphones": [], "loopbacks": [], "available": False}


def test_devices_endpoint_returns_enumerated_devices_when_available(client):
    c, _ = client
    fake_result = {
        "microphones": [{"id": "mic1", "name": "Built-in Microphone"}],
        "loopbacks": [{"id": "loop1", "name": "Speakers (loopback)"}],
        "available": True,
    }
    with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
        resp = c.get("/api/record/devices")
    assert resp.status_code == 200
    assert resp.json() == fake_result


def test_start_local_unavailable_when_soundcard_not_installed(client):
    import sys
    c, _ = client
    with patch.dict(sys.modules, {"soundcard": None}):
        resp = c.post("/api/record/start-local", json={"mic_id": "m", "system_id": "s"})
    assert resp.status_code == 503


def test_start_local_missing_ids_returns_400(client):
    c, _ = client
    fake_result = {"microphones": [], "loopbacks": [], "available": True}
    with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
        resp = c.post("/api/record/start-local", json={"mic_id": "", "system_id": "s"})
    assert resp.status_code == 400


def test_start_local_success_creates_local_recording(client):
    c, data_dir = client
    fake_result = {
        "microphones": [{"id": "mic1", "name": "Built-in Microphone"}],
        "loopbacks": [{"id": "loop1", "name": "Speakers (loopback)"}],
        "available": True,
    }
    mgr = _scripted_local_capture_manager(data_dir)
    c.app.state.local_capture_manager = mgr
    try:
        with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
            resp = c.post(
                "/api/record/start-local",
                json={"mic_id": "mic1", "system_id": "loop1", "campaign_slug": None},
            )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "recording"
        assert data["source"] == "local"
        assert data["devices"] == {"mic": "Built-in Microphone", "system": "Speakers (loopback)"}
    finally:
        mgr.stop_session()


def test_start_local_json_api_accepts_and_trims_name(client):
    c, data_dir = client
    fake_result = {
        "microphones": [{"id": "mic1", "name": "Mic"}],
        "loopbacks": [{"id": "loop1", "name": "Loop"}],
        "available": True,
    }
    mgr = _scripted_local_capture_manager(data_dir)
    c.app.state.local_capture_manager = mgr
    try:
        with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
            resp = c.post(
                "/api/record/start-local",
                json={"mic_id": "mic1", "system_id": "loop1", "name": "  Session 14 — the ambush  "},
            )
        assert resp.status_code == 201
        assert resp.json()["name"] == "Session 14 — the ambush"
    finally:
        mgr.stop_session()


def test_start_local_json_api_blank_name_stored_as_none(client):
    c, data_dir = client
    fake_result = {
        "microphones": [{"id": "mic1", "name": "Mic"}],
        "loopbacks": [{"id": "loop1", "name": "Loop"}],
        "available": True,
    }
    mgr = _scripted_local_capture_manager(data_dir)
    c.app.state.local_capture_manager = mgr
    try:
        with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
            resp = c.post(
                "/api/record/start-local",
                json={"mic_id": "mic1", "system_id": "loop1", "name": "   "},
            )
        assert resp.json()["name"] is None
    finally:
        mgr.stop_session()


def test_start_local_html_form_accepts_name(client):
    c, data_dir = client
    fake_result = {
        "microphones": [{"id": "mic1", "name": "Mic"}],
        "loopbacks": [{"id": "loop1", "name": "Loop"}],
        "available": True,
    }
    mgr = _scripted_local_capture_manager(data_dir)
    c.app.state.local_capture_manager = mgr
    try:
        with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
            c.post(
                "/record/start-local",
                data={"mic_id": "mic1", "system_id": "loop1", "name": "Game night"},
            )
        assert mgr.active_recording.name == "Game night"
    finally:
        mgr.stop_session()


def test_clean_session_name_caps_length():
    from wisper_transcribe.web.routes.record import _MAX_SESSION_NAME_LEN, _clean_session_name

    result = _clean_session_name("x" * 500)
    assert len(result) == _MAX_SESSION_NAME_LEN


def test_clean_session_name_blank_and_whitespace_only_is_none():
    from wisper_transcribe.web.routes.record import _clean_session_name

    assert _clean_session_name("") is None
    assert _clean_session_name("   ") is None


def test_stop_local_with_no_active_session_returns_400(client):
    c, data_dir = client
    c.app.state.local_capture_manager = _scripted_local_capture_manager(data_dir)
    resp = c.post("/api/record/stop-local")
    assert resp.status_code == 400


def test_stop_local_stops_active_session(client):
    c, data_dir = client
    fake_result = {
        "microphones": [{"id": "mic1", "name": "Mic"}],
        "loopbacks": [{"id": "loop1", "name": "Loop"}],
        "available": True,
    }
    mgr = _scripted_local_capture_manager(data_dir)
    c.app.state.local_capture_manager = mgr
    with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
        start_resp = c.post("/api/record/start-local", json={"mic_id": "mic1", "system_id": "loop1"})
    assert start_resp.status_code == 201

    stop_resp = c.post("/api/record/stop-local")
    assert stop_resp.status_code == 200
    data = stop_resp.json()
    assert data["status"] == "completed"


def test_start_local_unavailable_when_manager_not_wired(client):
    c, _ = client
    c.app.state.local_capture_manager = None
    resp = c.post("/api/record/start-local", json={"mic_id": "m", "system_id": "s"})
    assert resp.status_code == 503


def test_devices_endpoint_missing_id_field_returns_400(client):
    c, _ = client
    fake_result = {"microphones": [], "loopbacks": [], "available": True}
    with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
        resp = c.post("/api/record/start-local", json={})
    assert resp.status_code == 400


def test_cross_manager_local_active_blocks_discord_start(client):
    """A live local session must 409 an attempt to start a Discord session,
    and vice versa (mutual exclusion across managers)."""
    c, data_dir = client
    fake_result = {
        "microphones": [{"id": "mic1", "name": "Mic"}],
        "loopbacks": [{"id": "loop1", "name": "Loop"}],
        "available": True,
    }
    mgr = _scripted_local_capture_manager(data_dir)
    c.app.state.local_capture_manager = mgr
    try:
        with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
            start_resp = c.post("/api/record/start-local", json={"mic_id": "mic1", "system_id": "loop1"})
        assert start_resp.status_code == 201

        resp = c.post("/api/record/start", json={"voice_channel_id": "123", "guild_id": "G1"})
        assert resp.status_code == 409
    finally:
        mgr.stop_session()


def test_cross_manager_discord_active_blocks_local_start(client):
    """A fake "already active" BotManager stands in for the real one here:
    the real BotManager.start_session() launches an async session loop that,
    with no Discord token configured in the test environment, races to
    mark the recording 'failed' almost immediately -- flaky as a setup
    precondition for a check that only cares about `.active_recording`."""
    from wisper_transcribe.recording_manager import create_recording

    c, data_dir = client
    fake_discord_rec = create_recording(voice_channel_id="123", guild_id="G1", data_dir=data_dir)

    class _FakeBotManager:
        active_recording = fake_discord_rec

    c.app.state.bot_manager = _FakeBotManager()

    fake_result = {
        "microphones": [{"id": "mic1", "name": "Mic"}],
        "loopbacks": [{"id": "loop1", "name": "Loop"}],
        "available": True,
    }
    mgr = _scripted_local_capture_manager(data_dir)
    c.app.state.local_capture_manager = mgr
    with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
        resp = c.post("/api/record/start-local", json={"mic_id": "mic1", "system_id": "loop1"})
    assert resp.status_code == 409


def test_current_active_recording_reports_active_local_session(client):
    """R: /record/sse and the /record page used to read bm.active_recording
    only, so a live local session reported 'idle' -- the shared
    _current_active_recording() resolver (used by both) fixes that.

    Exercised directly against the resolver rather than over a live
    `/record/sse` HTTP stream: that endpoint polls forever
    (`while True: ... await asyncio.sleep(1.0)`), and TestClient's
    synchronous streaming wrapper has no clean way to read "just the first
    event" without risking a hang.
    """
    from wisper_transcribe.web.routes.record import _current_active_recording

    c, data_dir = client
    fake_result = {
        "microphones": [{"id": "mic1", "name": "Mic"}],
        "loopbacks": [{"id": "loop1", "name": "Loop"}],
        "available": True,
    }
    mgr = _scripted_local_capture_manager(data_dir)
    c.app.state.local_capture_manager = mgr
    try:
        with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
            c.post("/api/record/start-local", json={"mic_id": "mic1", "system_id": "loop1"})

        class _FakeRequest:
            app = c.app

        rec = _current_active_recording(_FakeRequest())
        assert rec is not None
        assert rec.status == "recording"
        assert rec.source == "local"
    finally:
        mgr.stop_session()


def test_record_page_shows_local_active_session(client):
    c, data_dir = client
    fake_result = {
        "microphones": [{"id": "mic1", "name": "Mic"}],
        "loopbacks": [{"id": "loop1", "name": "Loop"}],
        "available": True,
    }
    mgr = _scripted_local_capture_manager(data_dir)
    c.app.state.local_capture_manager = mgr
    try:
        with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
            c.post("/api/record/start-local", json={"mic_id": "mic1", "system_id": "loop1"})
            resp = c.get("/record")
        assert resp.status_code == 200
        assert "Local capture" in resp.text
    finally:
        mgr.stop_session()


class _FakeBotManagerWithActiveRecording:
    def __init__(self, recording):
        self.active_recording = recording


def test_record_page_hides_speaker_meters_for_local_session(client):
    """A local session's `discord_speakers` dict is never populated (that's
    a Discord-bot-only auto-tag mechanism) -- the 'Voices · live' / 'At the
    table' section, and its 'Participants will appear as they join' promise,
    must not render at all for source == 'local', since it can never have
    anything to show."""
    c, data_dir = client
    fake_result = {
        "microphones": [{"id": "mic1", "name": "Mic"}],
        "loopbacks": [{"id": "loop1", "name": "Loop"}],
        "available": True,
    }
    mgr = _scripted_local_capture_manager(data_dir)
    c.app.state.local_capture_manager = mgr
    try:
        with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
            c.post("/api/record/start-local", json={"mic_id": "mic1", "system_id": "loop1"})
            resp = c.get("/record")
        assert resp.status_code == 200
        assert "At the table" not in resp.text
        assert "No speakers mapped yet" not in resp.text
    finally:
        mgr.stop_session()


def test_record_page_shows_speaker_meters_for_discord_session(client):
    c, data_dir = client
    from wisper_transcribe.recording_manager import create_recording

    rec = create_recording("VC1", "G1", data_dir=data_dir, source="discord")
    c.app.state.bot_manager = _FakeBotManagerWithActiveRecording(rec)

    resp = c.get("/record")
    assert resp.status_code == 200
    assert "At the table" in resp.text
    assert "No speakers mapped yet" in resp.text


def test_record_page_wires_live_ticker_to_recording_live_sse(client):
    """The 'Heard so far' ticker on /record subscribes to the same working
    SSE stream as the recording detail page, not the dead partial_transcript
    event that /record/sse never emits."""
    c, data_dir = client
    fake_result = {
        "microphones": [{"id": "mic1", "name": "Mic"}],
        "loopbacks": [{"id": "loop1", "name": "Loop"}],
        "available": True,
    }
    mgr = _scripted_local_capture_manager(data_dir)
    c.app.state.local_capture_manager = mgr
    try:
        with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
            start_resp = c.post("/api/record/start-local", json={"mic_id": "mic1", "system_id": "loop1"})
            recording_id = start_resp.json()["id"]
            resp = c.get("/record")
        assert resp.status_code == 200
        assert f"/recordings/{recording_id}/live" in resp.text
        assert "partial_transcript" not in resp.text
    finally:
        mgr.stop_session()


def test_record_page_hides_local_section_when_unavailable(client):
    """No Local card at all when soundcard isn't importable (the default
    in this test environment, but pinned explicitly for determinism)."""
    c, _ = client
    fake_result = {"microphones": [], "loopbacks": [], "available": False}
    with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
        resp = c.get("/record")
    assert resp.status_code == 200
    assert "Start local recording" not in resp.text


def test_record_page_shows_local_section_with_device_options(client):
    c, _ = client
    fake_result = {
        "microphones": [{"id": "mic1", "name": "Built-in Microphone"}],
        "loopbacks": [{"id": "loop1", "name": "Speakers (loopback)"}],
        "available": True,
    }
    with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result):
        resp = c.get("/record")
    assert resp.status_code == 200
    assert "Start local recording" in resp.text
    assert "Built-in Microphone" in resp.text
    assert "Speakers (loopback)" in resp.text


def test_record_page_hides_this_is_me_when_no_profiles(client):
    c, _ = client
    fake_result = {"microphones": [], "loopbacks": [], "available": True}
    with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_result), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        resp = c.get("/record")
    assert 'name="mic_profile_key"' not in resp.text


def test_record_page_shows_this_is_me_dropdown_with_enrolled_profiles(client):
    from wisper_transcribe.models import SpeakerProfile

    c, data_dir = client
    fake_devices = {"microphones": [], "loopbacks": [], "available": True}
    fake_profile = SpeakerProfile(
        name="brandon", display_name="Brandon", role="player",
        embedding_path=data_dir / "brandon.npy", enrolled_date="2026-01-01",
        enrollment_source="test",
    )
    with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_devices), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={"brandon": fake_profile}):
        resp = c.get("/record")
    assert 'name="mic_profile_key"' in resp.text
    assert "Brandon" in resp.text
    assert 'This is me' in resp.text


def test_record_page_preselects_default_mic_profile(client):
    """The mic-profile dropdown pre-selects `default_mic_profile_key` from config."""
    from wisper_transcribe.models import SpeakerProfile

    c, data_dir = client
    fake_devices = {"microphones": [], "loopbacks": [], "available": True}
    fake_profile = SpeakerProfile(
        name="brandon", display_name="Brandon", role="player",
        embedding_path=data_dir / "brandon.npy", enrolled_date="2026-01-01",
        enrollment_source="test",
    )
    with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_devices), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={"brandon": fake_profile}), \
         patch("wisper_transcribe.web.routes.record.load_config",
               return_value={"default_mic_profile_key": "brandon"}):
        resp = c.get("/record")
    assert '<option value="brandon" selected>Brandon</option>' in resp.text


def test_start_local_remembers_mic_profile_default(client):
    """Starting a local session with a mic profile persists it as the next default."""
    c, data_dir = client
    fake_devices = {
        "microphones": [{"id": "mic1", "name": "Mic"}],
        "loopbacks": [{"id": "loop1", "name": "Loop"}],
        "available": True,
    }
    mgr = _scripted_local_capture_manager(data_dir)
    c.app.state.local_capture_manager = mgr
    try:
        with patch("wisper_transcribe.web.routes.record.enumerate_devices", return_value=fake_devices):
            resp = c.post(
                "/api/record/start-local",
                json={"mic_id": "mic1", "system_id": "loop1", "mic_profile_key": "brandon"},
            )
        assert resp.status_code == 201
    finally:
        mgr.stop_session()

    from wisper_transcribe.config import load_config
    assert load_config()["default_mic_profile_key"] == "brandon"


def test_recordings_list_shows_local_badge(client):
    from wisper_transcribe.recording_manager import create_recording

    c, data_dir = client
    create_recording(
        voice_channel_id="", guild_id="", data_dir=data_dir,
        source="local", devices={"mic": "Mic", "system": "Speakers"},
    )
    resp = c.get("/recordings")
    assert resp.status_code == 200
    assert "LOCAL" in resp.text


def test_recording_detail_shows_local_device_names(client):
    from wisper_transcribe.recording_manager import create_recording

    c, data_dir = client
    rec = create_recording(
        voice_channel_id="", guild_id="", data_dir=data_dir,
        source="local", devices={"mic": "Built-in Microphone", "system": "Speakers (loopback)"},
    )
    resp = c.get(f"/recordings/{rec.id}")
    assert resp.status_code == 200
    assert "Built-in Microphone" in resp.text
    assert "Speakers (loopback)" in resp.text
    assert "LOCAL" in resp.text


def test_recording_detail_shows_live_pane_for_active_local_session(client):
    from wisper_transcribe.recording_manager import create_recording

    c, data_dir = client
    rec = create_recording(voice_channel_id="", guild_id="", data_dir=data_dir, source="local")
    resp = c.get(f"/recordings/{rec.id}")
    assert resp.status_code == 200
    assert "live-transcript" in resp.text
    assert f"/recordings/{rec.id}/live" in resp.text


def test_recording_detail_hides_live_pane_when_completed(client):
    from wisper_transcribe.recording_manager import create_recording, update_recording_status

    c, data_dir = client
    rec = create_recording(voice_channel_id="", guild_id="", data_dir=data_dir, source="local")
    update_recording_status(rec.id, "completed", data_dir)
    resp = c.get(f"/recordings/{rec.id}")
    assert resp.status_code == 200
    assert "live-transcript" not in resp.text


def test_recording_detail_hides_live_pane_for_discord_source(client):
    from wisper_transcribe.recording_manager import create_recording

    c, data_dir = client
    rec = create_recording(voice_channel_id="VC1", guild_id="G1", data_dir=data_dir)
    assert rec.source == "discord"
    resp = c.get(f"/recordings/{rec.id}")
    assert resp.status_code == 200
    assert "live-transcript" not in resp.text


def test_recording_detail_invalid_id_returns_400(client):
    c, _ = client
    resp = c.get("/api/recordings/../evil")
    assert resp.status_code in (400, 404)


def test_recording_detail_null_byte_returns_400(client):
    from urllib.parse import quote
    c, _ = client
    resp = c.get(f"/api/recordings/{quote('some%00name')}")
    assert resp.status_code in (400, 404)


def test_server_json_written_on_lifespan_startup(tmp_path):
    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path):
        import os
        os.environ["WISPER_BIND"] = "127.0.0.1:9999"
        from wisper_transcribe.web.app import create_app
        test_app = create_app()
        with TestClient(test_app):
            sj = tmp_path / "server.json"
            assert sj.exists(), "server.json should be written during lifespan startup"
            data = json.loads(sj.read_text())
            assert data["url"] == "http://127.0.0.1:9999"
    # cleanup env
    os.environ.pop("WISPER_BIND", None)


def test_server_json_deleted_on_lifespan_shutdown(tmp_path):
    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path):
        from wisper_transcribe.web.app import create_app
        test_app = create_app()
        with TestClient(test_app):
            pass  # exits context manager = shutdown
        sj = tmp_path / "server.json"
        assert not sj.exists(), "server.json should be deleted after lifespan shutdown"


# ---------------------------------------------------------------------------
# Phase 5 — HTML routes
# ---------------------------------------------------------------------------

def test_record_page_returns_200(client):
    c, _ = client
    resp = c.get("/record")
    assert resp.status_code == 200
    assert "Record" in resp.text


def test_recordings_list_returns_200_empty(client):
    c, _ = client
    resp = c.get("/recordings")
    assert resp.status_code == 200
    assert "Recordings" in resp.text
    assert "No recordings yet" in resp.text


def test_recordings_list_groups_by_campaign(client):
    c, tmp_path = client
    from wisper_transcribe.campaign_manager import create_campaign
    from wisper_transcribe.recording_manager import create_recording, update_recording_status
    create_campaign("Test Campaign", data_dir=tmp_path)
    rec = create_recording("VC1", "G1", campaign_slug="test-campaign", data_dir=tmp_path)
    update_recording_status(rec.id, "completed", data_dir=tmp_path)
    resp = c.get("/recordings")
    assert resp.status_code == 200
    assert "Test Campaign" in resp.text
    assert rec.id[:8] in resp.text


def test_recordings_list_handles_null_started_at(client):
    """R8 regression: sorting used `r.started_at or r.started_at`, a no-op
    that raises TypeError (None vs datetime comparison) as soon as any
    recording has started_at=None, 500ing the whole /recordings page."""
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording, save_recording

    rec_with_time = create_recording("VC1", "G1", data_dir=tmp_path)
    rec_no_time = create_recording("VC2", "G1", data_dir=tmp_path)
    rec_no_time.started_at = None
    save_recording(rec_no_time, data_dir=tmp_path)

    resp = c.get("/recordings")
    assert resp.status_code == 200
    assert rec_with_time.id[:8] in resp.text
    assert rec_no_time.id[:8] in resp.text


def test_recording_detail_returns_200(client):
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    resp = c.get(f"/recordings/{rec.id}")
    assert resp.status_code == 200
    assert rec.id in resp.text


def test_recording_detail_shows_retranscribe_button_when_transcribed(client):
    """The Re-transcribe button appears when status is 'transcribed'."""
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording, save_recording
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    rec.status = "transcribed"
    save_recording(rec, tmp_path)
    resp = c.get(f"/recordings/{rec.id}")
    assert resp.status_code == 200
    assert "Re-transcribe" in resp.text


def test_recording_detail_no_retranscribe_button_when_completed(client):
    """The Re-transcribe button does NOT appear for a plain 'completed' recording."""
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording, save_recording
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    rec.status = "completed"
    save_recording(rec, tmp_path)
    resp = c.get(f"/recordings/{rec.id}")
    assert resp.status_code == 200
    assert "Re-transcribe" not in resp.text


def test_recording_detail_shows_duration(client):
    """The status strip shows elapsed duration between started_at and ended_at."""
    from datetime import timedelta, timezone
    from wisper_transcribe.recording_manager import create_recording, save_recording

    c, tmp_path = client
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    rec.status = "completed"
    rec.ended_at = rec.started_at.astimezone(timezone.utc) + timedelta(minutes=1, seconds=57)
    save_recording(rec, tmp_path)
    resp = c.get(f"/recordings/{rec.id}")
    assert resp.status_code == 200
    assert "0:01:57" in resp.text


def test_recording_detail_duration_dash_when_not_ended(client):
    """No `ended_at` yet (still recording) shows a dash, not a bogus duration."""
    from wisper_transcribe.recording_manager import create_recording

    c, tmp_path = client
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    resp = c.get(f"/recordings/{rec.id}")
    assert resp.status_code == 200
    assert "Duration" in resp.text


def test_recording_detail_unknown_id_redirects(client):
    c, _ = client
    import uuid
    unknown_id = str(uuid.uuid4())
    resp = c.get(f"/recordings/{unknown_id}", follow_redirects=False)
    assert resp.status_code == 303
    assert "/recordings" in resp.headers["location"]


def test_recording_delete_removes_entry(client):
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording, load_recordings
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    resp = c.post(f"/recordings/{rec.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert load_recordings(tmp_path).get(rec.id) is None


def test_recording_live_streams_end_event_when_no_live_job(client):
    """Phase 2: GET /recordings/{id}/live is now a real SSE endpoint. A
    recording with no active JOB_LIVE session (never started local live
    transcription) just gets an immediate 'end' event and no snapshot
    (no live_transcript.md on disk) -- see tests/test_record_live_routes.py
    for the live-job-wired cases."""
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    with c.stream("GET", f"/recordings/{rec.id}/live") as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "event: end" in body


def test_recording_live_invalid_id_returns_400(client):
    c, _ = client
    resp = c.get("/recordings/../evil/live")
    assert resp.status_code in (400, 404)


# ---------------------------------------------------------------------------
# Phase 6 — enrollment routes
# ---------------------------------------------------------------------------

def test_enroll_unknown_speaker_enqueues_job(client):
    """R6: POST /recordings/{id}/enroll no longer runs the pydub decode +
    embedding extraction synchronously in the request — it enqueues a
    JOB_ENROLL job (mode "recording") carrying the validated parameters and
    redirects to the job detail page. The recording-state updates now happen
    in the job runner (covered in tests/test_web_jobs.py)."""
    from unittest.mock import patch

    from wisper_transcribe.recording_manager import create_recording, save_recording
    from wisper_transcribe.web.jobs import JOB_ENROLL, JobQueue

    c, tmp_path = client
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    rec.unbound_speakers = ["999999999999999999"]
    rec.discord_speakers["999999999999999999"] = ""
    save_recording(rec, tmp_path)

    # Neutralise the runner so the live worker (this fixture runs the app
    # lifespan) can't race the assertions below.
    with patch.object(JobQueue, "_run_enroll_job", lambda self, job: None):
        resp = c.post(
            f"/recordings/{rec.id}/enroll",
            data={"discord_user_id": "999999999999999999", "profile_name": "Bob"},
            follow_redirects=False,
        )

        assert resp.status_code == 303
        location = resp.headers["location"]
        assert location.startswith("/transcribe/jobs/")

        queue = c.app.state.job_queue
        job_id = location.rsplit("/", 1)[-1]
        job = queue.get(job_id)
        assert job is not None
        assert job.job_type == JOB_ENROLL
        assert job.enroll_mode == "recording"
        assert job.enroll_params["recording_id"] == rec.id
        assert job.enroll_params["discord_uid"] == "999999999999999999"
        assert job.enroll_params["profile_key"] == "bob"
        assert job.enroll_params["per_user_dir"].endswith("999999999999999999")


def test_enroll_unknown_speaker_invalid_id_returns_400(client):
    """POST /recordings/{id}/enroll with a non-numeric discord_user_id returns 400."""
    from wisper_transcribe.recording_manager import create_recording

    c, tmp_path = client
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    resp = c.post(
        f"/recordings/{rec.id}/enroll",
        data={"discord_user_id": "not-a-snowflake", "profile_name": "Bob"},
    )
    assert resp.status_code == 400


def test_enroll_already_bound_speaker_returns_409(client):
    """POST /recordings/{id}/enroll for a user not in unbound_speakers returns 409."""
    from wisper_transcribe.recording_manager import create_recording

    c, tmp_path = client
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    # User never added to unbound_speakers
    resp = c.post(
        f"/recordings/{rec.id}/enroll",
        data={"discord_user_id": "999999999999999999", "profile_name": "Bob"},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Phase 7 — transcribe hand-off
# ---------------------------------------------------------------------------


def test_transcribe_recording_handoff(client):
    """POST /recordings/{id}/transcribe queues a transcription job and updates status."""
    from wisper_transcribe.recording_manager import (
        create_recording,
        load_recordings,
        save_recording,
        update_recording_status,
    )

    c, tmp_path = client
    rec = create_recording("VC1", "G1", campaign_slug="my-game", data_dir=tmp_path)

    # Simulate a completed recording with a combined.wav on disk
    combined = tmp_path / "recordings" / rec.id / "final" / "combined.wav"
    combined.parent.mkdir(parents=True, exist_ok=True)
    combined.write_bytes(b"fake wav data")
    rec.combined_path = combined
    rec.status = "completed"
    save_recording(rec, tmp_path)

    # Mock job_queue.submit to avoid real transcription on fake audio
    from wisper_transcribe.web.jobs import Job as JobCls
    import uuid as _uuid
    fake_job = JobCls(
        id=str(_uuid.uuid4()),
        status="pending",
        created_at=rec.started_at,
        input_path=str(tmp_path / "output" / f"{rec.id}.wav"),
        kwargs={},
        name=rec.id,
    )
    with patch.object(
        c.app.state.job_queue, "submit", return_value=fake_job
    ) as mock_submit:
        resp = c.post(f"/recordings/{rec.id}/transcribe", follow_redirects=False)

    assert resp.status_code == 303
    assert f"/recordings/{rec.id}" in resp.headers["location"]

    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.status == "transcribing"
    assert loaded.job_id is not None
    assert loaded.job_id == fake_job.id

    # Verify the combined.wav was copied to output dir
    # get_data_dir is patched to tmp_path, so _default_output_dir() → tmp_path / "output"
    dest = tmp_path / "output" / f"{rec.id}.wav"
    assert dest.exists()
    mock_submit.assert_called_once()


def test_transcribe_recording_reverts_status_on_job_failure(client):
    """A failed transcription job (on_error callback) reverts the recording
    back to its pre-transcribe status instead of leaving it stuck at
    'transcribing' forever with no retry path in the UI."""
    from wisper_transcribe.recording_manager import create_recording, load_recordings, save_recording

    c, tmp_path = client
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    combined = tmp_path / "recordings" / rec.id / "final" / "combined.wav"
    combined.parent.mkdir(parents=True, exist_ok=True)
    combined.write_bytes(b"fake wav data")
    rec.combined_path = combined
    rec.status = "completed"
    save_recording(rec, tmp_path)

    from wisper_transcribe.web.jobs import Job as JobCls, FAILED
    import uuid as _uuid
    fake_job = JobCls(
        id=str(_uuid.uuid4()), status="pending", created_at=rec.started_at,
        input_path=str(tmp_path / "output" / f"{rec.id}.wav"), kwargs={}, name=rec.id,
    )

    with patch.object(c.app.state.job_queue, "submit", return_value=fake_job) as mock_submit:
        resp = c.post(f"/recordings/{rec.id}/transcribe", follow_redirects=False)
    assert resp.status_code == 303

    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.status == "transcribing"

    # Simulate the job failing: invoke the on_error callback the route wired up.
    on_error = mock_submit.call_args.kwargs["on_error"]
    fake_job.status = FAILED
    on_error(fake_job)

    reverted = load_recordings(tmp_path)[rec.id]
    assert reverted.status == "completed"


def test_retranscribe_recording_reverts_to_transcribed_on_job_failure(client):
    """A failed re-transcribe (starting from 'transcribed', not 'completed')
    reverts back to 'transcribed' -- keeping the old transcript's
    View/Re-transcribe actions available -- not to a bare 'completed'."""
    from wisper_transcribe.recording_manager import create_recording, load_recordings, save_recording

    c, tmp_path = client
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    combined = tmp_path / "recordings" / rec.id / "final" / "combined.wav"
    combined.parent.mkdir(parents=True, exist_ok=True)
    combined.write_bytes(b"fake wav data")
    rec.combined_path = combined
    rec.status = "transcribed"
    save_recording(rec, tmp_path)

    from wisper_transcribe.web.jobs import Job as JobCls, FAILED
    import uuid as _uuid
    fake_job = JobCls(
        id=str(_uuid.uuid4()), status="pending", created_at=rec.started_at,
        input_path=str(tmp_path / "output" / f"{rec.id}.wav"), kwargs={}, name=rec.id,
    )

    with patch.object(c.app.state.job_queue, "submit", return_value=fake_job) as mock_submit:
        c.post(f"/recordings/{rec.id}/transcribe", follow_redirects=False)

    on_error = mock_submit.call_args.kwargs["on_error"]
    fake_job.status = FAILED
    on_error(fake_job)

    reverted = load_recordings(tmp_path)[rec.id]
    assert reverted.status == "transcribed"


def test_transcribe_recording_not_completed_rejects(client):
    """POST /recordings/{id}/transcribe rejects recordings not in completed status."""
    from wisper_transcribe.recording_manager import create_recording

    c, tmp_path = client
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    # Still in 'recording' status

    resp = c.post(f"/recordings/{rec.id}/transcribe", follow_redirects=False)
    assert resp.status_code == 303
    assert "error=not_ready" in resp.headers["location"]


def test_transcribe_recording_no_audio_rejects(client):
    """POST /recordings/{id}/transcribe rejects recordings with no combined_path."""
    from wisper_transcribe.recording_manager import create_recording, save_recording

    c, tmp_path = client
    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    rec.status = "completed"
    rec.combined_path = None  # no audio
    save_recording(rec, tmp_path)

    resp = c.post(f"/recordings/{rec.id}/transcribe", follow_redirects=False)
    assert resp.status_code == 303
    assert "error=no_audio" in resp.headers["location"]


def test_transcribe_recording_no_audio_regression_after_real_bot_session(client):
    """R2 end-to-end regression: before the fix, `Recording.combined_path`
    was assigned exactly once, to None, and never populated by a real
    BotManager session, so this hand-off ALWAYS redirected with
    ?error=no_audio — even for a session that actually captured audio.
    Runs a real BotManager session (fake audio source, no JDA/socket) that
    captures a `__mixed__` combined track, then verifies the hand-off route
    accepts it."""
    from wisper_transcribe.recording_manager import load_recordings
    from wisper_transcribe.web.discord_bot import BotManager
    from wisper_transcribe.web.jobs import Job as JobCls
    from tests._discord_fakes import make_pcm_frame, scripted_source
    import uuid as _uuid

    c, tmp_path = client

    frames = [("__mixed__", make_pcm_frame())] * 5
    factory = scripted_source(frames)
    bm = BotManager(data_dir=tmp_path, audio_source_factory=factory)

    async def _run_session():
        bm.start()
        rec = await bm.start_session(None, "VC1", "G1")
        await asyncio.wait_for(bm._task, timeout=5)
        return rec

    rec = asyncio.run(_run_session())

    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.status == "completed"
    assert loaded.combined_path is not None, "R2: combined_path should be populated"
    assert loaded.combined_path.exists()

    fake_job = JobCls(
        id=str(_uuid.uuid4()),
        status="pending",
        created_at=loaded.started_at,
        input_path=str(tmp_path / "output" / f"{rec.id}.wav"),
        kwargs={},
        name=rec.id,
    )
    with patch.object(c.app.state.job_queue, "submit", return_value=fake_job) as mock_submit:
        resp = c.post(f"/recordings/{rec.id}/transcribe", follow_redirects=False)

    assert resp.status_code == 303
    assert "error=no_audio" not in resp.headers["location"]
    assert f"/recordings/{rec.id}" in resp.headers["location"]
    mock_submit.assert_called_once()

    dest = tmp_path / "output" / f"{rec.id}.wav"
    assert dest.exists()
    with wave.open(str(dest), "rb") as wf:
        assert wf.getframerate() == 16000
        assert wf.getnchannels() == 1
        assert wf.getnframes() > 0


def test_transcribe_recording_invalid_id_blocked(client):
    """POST /recordings/{id}/transcribe blocks traversal payloads."""
    c, _ = client
    resp = c.post("/recordings/../evil/transcribe", follow_redirects=False)
    assert resp.status_code in (400, 404)


# ---------------------------------------------------------------------------
# R7 — JSON API: /api/recordings (list/detail/transcribe/delete)
#
# These back `wisper record list/show/transcribe/delete` (cli.py); they used
# to be 501 stubs so every documented subcommand except start/stop always
# failed with "Server returned 501".
# ---------------------------------------------------------------------------

def test_api_recordings_list_empty(client):
    c, _ = client
    resp = c.get("/api/recordings")
    assert resp.status_code == 200
    assert resp.json() == {"recordings": []}


def test_api_recordings_list_returns_recordings(client):
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording

    rec = create_recording("VC1", "G1", campaign_slug="my-game", data_dir=tmp_path)
    resp = c.get("/api/recordings")
    assert resp.status_code == 200
    data = resp.json()["recordings"]
    assert len(data) == 1
    assert data[0]["id"] == rec.id
    assert data[0]["campaign_slug"] == "my-game"
    assert data[0]["status"] == "recording"
    # No filesystem paths leaked.
    assert "combined_path" not in data[0]
    assert "per_user_dir" not in data[0]
    assert "transcript_path" not in data[0]


def test_api_recordings_list_filters_by_campaign(client):
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording

    rec1 = create_recording("VC1", "G1", campaign_slug="game-a", data_dir=tmp_path)
    create_recording("VC2", "G1", campaign_slug="game-b", data_dir=tmp_path)

    resp = c.get("/api/recordings", params={"campaign": "game-a"})
    assert resp.status_code == 200
    data = resp.json()["recordings"]
    assert len(data) == 1
    assert data[0]["id"] == rec1.id


def test_api_recording_detail_returns_200(client):
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording

    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    resp = c.get(f"/api/recordings/{rec.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == rec.id
    assert data["status"] == "recording"
    assert "combined_path" not in data


def test_api_recording_detail_unknown_id_returns_404(client):
    c, _ = client
    import uuid
    resp = c.get(f"/api/recordings/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not_found"}


def test_api_recording_delete_removes_entry(client):
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording, load_recordings

    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    resp = c.post(f"/api/recordings/{rec.id}/delete")
    assert resp.status_code == 200
    assert resp.json() == {"id": rec.id, "deleted": True}
    assert load_recordings(tmp_path).get(rec.id) is None


def test_api_recording_delete_unknown_id_returns_404(client):
    c, _ = client
    import uuid
    resp = c.post(f"/api/recordings/{uuid.uuid4()}/delete")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not_found"}


def test_api_recording_transcribe_handoff(client):
    """POST /api/recordings/{id}/transcribe submits the same job the HTML
    hand-off route submits and returns the job id as JSON."""
    from wisper_transcribe.recording_manager import create_recording, load_recordings, save_recording
    from wisper_transcribe.web.jobs import Job as JobCls
    import uuid as _uuid

    c, tmp_path = client
    rec = create_recording("VC1", "G1", campaign_slug="my-game", data_dir=tmp_path)

    combined = tmp_path / "recordings" / rec.id / "final" / "combined.wav"
    combined.parent.mkdir(parents=True, exist_ok=True)
    combined.write_bytes(b"fake wav data")
    rec.combined_path = combined
    rec.status = "completed"
    save_recording(rec, tmp_path)

    fake_job = JobCls(
        id=str(_uuid.uuid4()),
        status="pending",
        created_at=rec.started_at,
        input_path=str(tmp_path / "output" / f"{rec.id}.wav"),
        kwargs={},
        name=rec.id,
    )
    with patch.object(c.app.state.job_queue, "submit", return_value=fake_job) as mock_submit:
        resp = c.post(f"/api/recordings/{rec.id}/transcribe")

    assert resp.status_code == 202
    data = resp.json()
    assert data["id"] == rec.id
    assert data["job_id"] == fake_job.id
    assert data["status"] == "transcribing"

    loaded = load_recordings(tmp_path)[rec.id]
    assert loaded.status == "transcribing"
    assert loaded.job_id == fake_job.id
    mock_submit.assert_called_once()


def test_api_recording_transcribe_not_ready_returns_409(client):
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording

    rec = create_recording("VC1", "G1", data_dir=tmp_path)  # still "recording"
    resp = c.post(f"/api/recordings/{rec.id}/transcribe")
    assert resp.status_code == 409
    assert resp.json() == {"error": "not_ready"}


def test_api_recording_transcribe_no_audio_returns_422(client):
    c, tmp_path = client
    from wisper_transcribe.recording_manager import create_recording, save_recording

    rec = create_recording("VC1", "G1", data_dir=tmp_path)
    rec.status = "completed"
    rec.combined_path = None
    save_recording(rec, tmp_path)

    resp = c.post(f"/api/recordings/{rec.id}/transcribe")
    assert resp.status_code == 422
    assert resp.json() == {"error": "no_audio"}


def test_api_recording_transcribe_unknown_id_returns_404(client):
    c, _ = client
    import uuid
    resp = c.post(f"/api/recordings/{uuid.uuid4()}/transcribe")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not_found"}
