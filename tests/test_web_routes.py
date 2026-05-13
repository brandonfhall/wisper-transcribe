"""Tests for web route handlers using FastAPI TestClient."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def app():
    """Fresh FastAPI app per test — no lifespan (background worker not started)."""
    from wisper_transcribe.web.app import create_app
    return create_app()


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient
    # Don't start the lifespan (avoids background asyncio worker in tests)
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_returns_200(client, tmp_path):
    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.web.routes.dashboard.load_config", return_value={}), \
         patch("wisper_transcribe.web.routes.dashboard.get_device", return_value="cpu"), \
         patch("wisper_transcribe.web.routes.dashboard.get_data_dir", return_value=str(tmp_path)):
        resp = client.get("/")
    assert resp.status_code == 200
    assert b"wisper" in resp.content


def test_dashboard_shows_llm_provider_and_model(client, tmp_path, monkeypatch):
    """System card surfaces the configured LLM provider and resolved model name."""
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = {
        "llm_provider": "ollama",
        "llm_model": "llama3.1:8b",
        "llm_endpoint": "http://localhost:11434",
    }
    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.web.routes.dashboard.load_config", return_value=cfg), \
         patch("wisper_transcribe.web.routes.dashboard.get_device", return_value="cpu"), \
         patch("wisper_transcribe.web.routes.dashboard.get_data_dir", return_value=str(tmp_path)):
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "LLM Provider" in body
    assert "ollama" in body
    assert "llama3.1:8b" in body
    # Local providers are always "Ready" — no key needed
    assert "Ready" in body


def test_dashboard_flags_cloud_provider_missing_key(client, tmp_path, monkeypatch):
    """A cloud provider with no env/config key shows the 'API key missing' hint."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = {
        "llm_provider": "anthropic",
        "llm_model": "",                 # blank → resolves to per-provider default
        "anthropic_api_key": "",
    }
    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.web.routes.dashboard.load_config", return_value=cfg), \
         patch("wisper_transcribe.web.routes.dashboard.get_device", return_value="cpu"), \
         patch("wisper_transcribe.web.routes.dashboard.get_data_dir", return_value=str(tmp_path)):
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "anthropic" in body
    assert "API key missing" in body
    # Resolved default model is shown when llm_model is blank
    assert "claude-sonnet-4-6" in body


def test_dashboard_jobs_partial_returns_200(client):
    resp = client.get("/jobs")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Transcribe form
# ---------------------------------------------------------------------------


def test_transcribe_get_returns_200(client):
    resp = client.get("/transcribe")
    assert resp.status_code == 200
    assert b"audio" in resp.content.lower()


def test_transcribe_post_queues_job_and_redirects(client, tmp_path):
    """Posting a file to /transcribe creates a job and redirects to job detail."""
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"fake mp3")

    with open(audio_file, "rb") as f:
        resp = client.post(
            "/transcribe",
            files={"file": ("test.mp3", f, "audio/mpeg")},
            data={"model_size": "tiny", "language": "en", "device": "cpu"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/transcribe/jobs/")


@pytest.mark.parametrize("filename,mime", [
    ("session.mp4",  "video/mp4"),
    ("session.mkv",  "video/x-matroska"),
    ("session.mov",  "video/quicktime"),
    ("session.webm", "video/webm"),
])
def test_transcribe_post_accepts_video_upload(client, tmp_path, filename, mime):
    """Video files are accepted by the upload route and queued as jobs."""
    video_file = tmp_path / filename
    video_file.write_bytes(b"fake video")
    with open(video_file, "rb") as f:
        resp = client.post(
            "/transcribe",
            files={"file": (filename, f, mime)},
            data={},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/transcribe/jobs/")


def test_transcribe_output_dir_field_is_ignored(client, tmp_path):
    """output_dir is no longer a form parameter — posting it must not cause a 422."""
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"fake mp3")
    with open(audio_file, "rb") as f:
        resp = client.post(
            "/transcribe",
            files={"file": ("test.mp3", f, "audio/mpeg")},
            data={"output_dir": "/etc/passwd"},
            follow_redirects=False,
        )
    # Unknown form fields are silently ignored; job must queue normally
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/transcribe/jobs/")


def test_job_detail_returns_200(client, tmp_path):
    """Job detail page renders correctly for an existing job."""
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"fake")
    with open(audio_file, "rb") as f:
        post_resp = client.post(
            "/transcribe",
            files={"file": ("test.mp3", f, "audio/mpeg")},
            data={},
            follow_redirects=False,
        )
    job_url = post_resp.headers["location"]
    resp = client.get(job_url)
    assert resp.status_code == 200


def test_job_detail_unknown_returns_404(client):
    resp = client.get("/transcribe/jobs/nonexistent-id-xyz")
    assert resp.status_code == 404


def test_job_detail_nav_contains_config_link(client, tmp_path):
    """The Config nav link must be present on the job detail page (sticky nav regression guard)."""
    audio_file = tmp_path / "nav_test.mp3"
    audio_file.write_bytes(b"fake")
    with open(audio_file, "rb") as f:
        post_resp = client.post(
            "/transcribe",
            files={"file": ("nav_test.mp3", f, "audio/mpeg")},
            data={},
            follow_redirects=False,
        )
    job_url = post_resp.headers["location"]
    resp = client.get(job_url)
    assert resp.status_code == 200
    assert b'href="/config"' in resp.content


def test_cancel_job_redirects(client, tmp_path):
    """POST /transcribe/jobs/<id>/cancel marks job cancelled and redirects."""
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"fake")
    with open(audio_file, "rb") as f:
        post_resp = client.post(
            "/transcribe",
            files={"file": ("test.mp3", f, "audio/mpeg")},
            data={},
            follow_redirects=False,
        )
    job_url = post_resp.headers["location"]
    job_id = job_url.split("/")[-1]

    cancel_resp = client.post(f"/transcribe/jobs/{job_id}/cancel", follow_redirects=False)
    assert cancel_resp.status_code == 303
    assert cancel_resp.headers["location"] == job_url


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------


def test_transcripts_list_empty(client, tmp_path):
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.get("/transcripts")
    assert resp.status_code == 200


def test_transcripts_list_shows_files(client, tmp_path):
    md = tmp_path / "session01.md"
    md.write_text("---\ntitle: Session 01\n---\n\n**Alice**: Hello.")
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.get("/transcripts")
    assert resp.status_code == 200
    assert b"session01" in resp.content


def test_transcript_detail_returns_200(client, tmp_path):
    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\ndate_processed: '2026-04-07'\nduration: '1:00:00'\n"
        "speakers:\n  - name: Alice\n    role: DM\n---\n\n**Alice** *(00:00)*: Hello."
    )
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.get("/transcripts/session01")
    assert resp.status_code == 200
    assert b"Session 01" in resp.content


def test_transcript_detail_not_found(client, tmp_path):
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.get("/transcripts/nonexistent")
    assert resp.status_code == 404


def test_transcript_detail_invalid_name_rejected(client):
    # Path traversal attempts are rejected (400) or normalised away (404)
    resp = client.get("/transcripts/../../etc")
    assert resp.status_code in (400, 404, 422)


def test_transcript_detail_unicode_filename(client, tmp_path):
    """Filenames with spaces, em-dashes, and special chars should work (400 fix)."""
    stem = "Episode 2 \u2013 O Captain! My (Dead) Captain!"
    md = tmp_path / f"{stem}.md"
    md.write_text(
        "---\ntitle: Episode 2\nspeakers:\n  - name: Alice\n    role: DM\n---\n\n**Alice**: Hello.",
        encoding="utf-8",
    )
    from urllib.parse import quote
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.get(f"/transcripts/{quote(stem)}")
    assert resp.status_code == 200
    assert b"Episode 2" in resp.content


def test_fix_speaker_unicode_filename_no_latin1_error(client, tmp_path):
    """POST /fix-speaker redirect must not raise UnicodeEncodeError for non-ASCII names."""
    stem = "Episode 2 \u2013 O Captain! My (Dead) Captain!"
    md = tmp_path / f"{stem}.md"
    md.write_text(
        "---\nspeakers:\n  - name: SPEAKER_00\n    role: ''\n---\n\n**SPEAKER_00**: Hello.",
        encoding="utf-8",
    )
    from urllib.parse import quote
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post(
            f"/transcripts/{quote(stem)}/fix-speaker",
            data={"old_name": "SPEAKER_00", "new_name": "Alice"},
            follow_redirects=False,
        )
    # Must not 500; redirect Location header must be ASCII-safe (percent-encoded)
    assert resp.status_code == 303
    location = resp.headers["location"]
    location.encode("latin-1")  # would raise if non-ASCII slipped through


def test_transcript_download(client, tmp_path):
    md = tmp_path / "session01.md"
    md.write_text("# Session 01\n\n**Alice**: Hello.")
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.get("/transcripts/session01/download")
    assert resp.status_code == 200
    assert b"Session 01" in resp.content


def test_delete_transcript(client, tmp_path):
    md = tmp_path / "session01.md"
    md.write_text("# Session 01\n\n**Alice**: Hello.")
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post("/transcripts/session01/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/transcripts"
    assert not md.exists()


def test_delete_transcript_nonexistent_is_silent(client, tmp_path):
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post("/transcripts/nonexistent/delete", follow_redirects=False)
    assert resp.status_code == 303  # silently redirects even if file missing


def test_fix_speaker_renames_in_transcript(client, tmp_path):
    md = tmp_path / "session01.md"
    md.write_text(
        "---\nspeakers:\n  - name: SPEAKER_00\n    role: ''\n---\n"
        "\n**SPEAKER_00** *(00:00)*: Hello."
    )
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post(
            "/transcripts/session01/fix-speaker",
            data={"old_name": "SPEAKER_00", "new_name": "Alice"},
            follow_redirects=False,
        )
    assert resp.status_code in (303, 200)
    content = md.read_text()
    assert "Alice" in content
    assert "SPEAKER_00" not in content


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_get_returns_200(client):
    with patch("wisper_transcribe.web.routes.config.load_config", return_value={"model": "medium"}), \
         patch("wisper_transcribe.web.routes.config.get_config_path", return_value=Path("/tmp/config.toml")):
        resp = client.get("/config")
    assert resp.status_code == 200
    assert b"model" in resp.content


def test_config_post_saves_and_redirects(client):
    with patch("wisper_transcribe.web.routes.config.load_config", return_value={}), \
         patch("wisper_transcribe.web.routes.config.save_config") as mock_save:
        resp = client.post(
            "/config",
            data={"model": "large-v3", "device": "cpu"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "saved=1" in resp.headers["location"]
    mock_save.assert_called_once()


def test_config_post_bool_field_unchecked(client):
    """Unchecked checkbox should save as False, not missing key."""
    captured = {}

    def fake_save(cfg):
        captured.update(cfg)

    with patch("wisper_transcribe.web.routes.config.load_config", return_value={"vad_filter": True}), \
         patch("wisper_transcribe.web.routes.config.save_config", side_effect=fake_save):
        # Submit form WITHOUT the vad_filter checkbox
        client.post("/config", data={"model": "medium"}, follow_redirects=False)

    assert captured.get("vad_filter") is False


def test_config_llm_fields_visible(client):
    """LLM provider select and model input appear on the config page."""
    with patch("wisper_transcribe.web.routes.config.load_config",
               return_value={"llm_provider": "ollama", "llm_model": ""}), \
         patch("wisper_transcribe.web.routes.config.get_config_path",
               return_value=Path("/tmp/config.toml")):
        resp = client.get("/config")
    assert resp.status_code == 200
    assert b"llm_provider" in resp.content
    assert b"llm_model" in resp.content
    assert b"llm_endpoint" in resp.content
    assert b"llm_temperature" in resp.content
    assert b"anthropic_api_key" in resp.content


def test_config_post_saves_llm_fields(client):
    """Saving LLM provider, model, endpoint, and temperature persists correctly."""
    captured = {}

    def fake_save(cfg):
        captured.update(cfg)

    with patch("wisper_transcribe.web.routes.config.load_config", return_value={}), \
         patch("wisper_transcribe.web.routes.config.save_config", side_effect=fake_save):
        client.post(
            "/config",
            data={
                "llm_provider": "anthropic",
                "llm_model": "claude-sonnet-4-6",
                "llm_endpoint": "http://localhost:11434",
                "llm_temperature": "0.3",
            },
            follow_redirects=False,
        )

    assert captured.get("llm_provider") == "anthropic"
    assert captured.get("llm_model") == "claude-sonnet-4-6"
    assert captured.get("llm_temperature") == 0.3


def test_config_post_saves_api_key_when_non_empty(client):
    """A non-empty API key is saved to config."""
    captured = {}

    def fake_save(cfg):
        captured.update(cfg)

    with patch("wisper_transcribe.web.routes.config.load_config", return_value={}), \
         patch("wisper_transcribe.web.routes.config.save_config", side_effect=fake_save):
        client.post(
            "/config",
            data={"anthropic_api_key": "sk-ant-test123"},
            follow_redirects=False,
        )

    assert captured.get("anthropic_api_key") == "sk-ant-test123"


def test_config_post_does_not_overwrite_api_key_with_empty(client):
    """An empty API key field must not overwrite an existing key."""
    captured = {}

    def fake_save(cfg):
        captured.update(cfg)

    existing = {"anthropic_api_key": "sk-ant-existing"}
    with patch("wisper_transcribe.web.routes.config.load_config", return_value=dict(existing)), \
         patch("wisper_transcribe.web.routes.config.save_config", side_effect=fake_save):
        # Submit with blank API key
        client.post(
            "/config",
            data={"anthropic_api_key": ""},
            follow_redirects=False,
        )

    assert captured.get("anthropic_api_key") == "sk-ant-existing"


def test_ollama_status_running_with_models(client):
    """Returns running=True and parsed model list when Ollama responds."""
    fake_body = {
        "models": [
            {"name": "gemma4:e4b", "size": 9_600_000_000},
            {"name": "qwen3.6:27b", "size": 17_000_000_000},
        ]
    }
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = fake_body

    with patch("wisper_transcribe.web.routes.config.load_config",
               return_value={"llm_endpoint": "http://localhost:11434"}), \
         patch("httpx.get", return_value=fake_resp):
        resp = client.get("/config/ollama-status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is True
    assert len(data["models"]) == 2
    assert data["models"][0]["name"] == "gemma4:e4b"
    assert "GB" in data["models"][0]["size"]


def test_ollama_status_not_reachable(client):
    """Returns running=False when Ollama cannot be reached."""
    import httpx as _httpx

    with patch("wisper_transcribe.web.routes.config.load_config", return_value={}), \
         patch("httpx.get", side_effect=_httpx.ConnectError("refused")):
        resp = client.get("/config/ollama-status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is False
    assert data["models"] == []


def test_ollama_status_running_no_models(client):
    """Returns running=True with empty list when Ollama is up but has no models."""
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {"models": []}

    with patch("wisper_transcribe.web.routes.config.load_config", return_value={}), \
         patch("httpx.get", return_value=fake_resp):
        resp = client.get("/config/ollama-status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is True
    assert data["models"] == []


def test_lmstudio_status_running_with_models(client):
    """Returns running=True and model list when LM Studio responds."""
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {"data": [{"id": "phi-3"}, {"id": "llama-3.2-1b"}]}

    with patch("wisper_transcribe.web.routes.config.load_config",
               return_value={"llm_endpoint": "http://localhost:1234"}), \
         patch("httpx.get", return_value=fake_resp):
        resp = client.get("/config/lmstudio-status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is True
    assert len(data["models"]) == 2
    assert data["models"][0]["name"] == "phi-3"


def test_lmstudio_status_not_reachable(client):
    """Returns running=False when LM Studio server is not running."""
    import httpx as _httpx

    with patch("wisper_transcribe.web.routes.config.load_config", return_value={}), \
         patch("httpx.get", side_effect=_httpx.ConnectError("refused")):
        resp = client.get("/config/lmstudio-status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is False
    assert data["models"] == []


def test_lmstudio_status_running_no_models(client):
    """Returns running=True with empty list when server is up but nothing loaded."""
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {"data": []}

    with patch("wisper_transcribe.web.routes.config.load_config", return_value={}), \
         patch("httpx.get", return_value=fake_resp):
        resp = client.get("/config/lmstudio-status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is True
    assert data["models"] == []


def test_lmstudio_status_uses_saved_config_endpoint(client):
    """Status check uses the saved config endpoint, not a query parameter."""
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {"data": []}

    with patch("wisper_transcribe.web.routes.config.load_config",
               return_value={"llm_endpoint": "http://myhost:1234"}), \
         patch("httpx.get", return_value=fake_resp) as mock_get:
        client.get("/config/lmstudio-status")

    mock_get.assert_called_once_with("http://myhost:1234/v1/models", timeout=3.0)


def test_ollama_status_uses_saved_config_endpoint(client):
    """Status check uses the saved config endpoint, not a query parameter."""
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {"models": []}

    with patch("wisper_transcribe.web.routes.config.load_config",
               return_value={"llm_endpoint": "http://myhost:11435"}), \
         patch("httpx.get", return_value=fake_resp) as mock_get:
        client.get("/config/ollama-status")

    mock_get.assert_called_once_with("http://myhost:11435/api/tags", timeout=3.0)


def test_ollama_cloud_catalog_running(client):
    """Returns running=True with parsed cloud-catalog model list."""
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {
        "models": [
            {"name": "gpt-oss:120b", "size": 65_000_000_000},
            {"name": "glm-4.7", "size": 696_000_000_000},
        ]
    }
    with patch("httpx.get", return_value=fake_resp) as mock_get:
        resp = client.get("/config/ollama-cloud-catalog")

    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is True
    assert {m["name"] for m in data["models"]} == {"gpt-oss:120b", "glm-4.7"}
    assert "GB" in data["models"][0]["size"]
    mock_get.assert_called_once_with("https://ollama.com/api/tags", timeout=5.0)


def test_ollama_cloud_catalog_network_error(client):
    """Network failure returns running=False with a generic error message."""
    import httpx as _httpx

    with patch("httpx.get", side_effect=_httpx.ConnectError("refused")):
        resp = client.get("/config/ollama-cloud-catalog")

    data = resp.json()
    assert data["running"] is False
    assert data["models"] == []
    assert data["error"] == "Could not reach ollama.com · check your network"


def _make_fake_sdk_page(items):
    """Build a SyncPage-like object with .data = items."""
    page = MagicMock()
    page.data = items
    return page


def test_anthropic_models_no_key_returns_error(client):
    """No env, no config, no form key → running=False with hint."""
    with patch("wisper_transcribe.web.routes.config.get_llm_api_key", return_value=None):
        resp = client.post("/config/anthropic-models")
    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is False
    assert "API key required" in data["error"]


def test_anthropic_models_running_with_models(client):
    """Returns chat models with display_name surfaced as `size`."""
    fake_models = [
        MagicMock(id="claude-sonnet-4-6", display_name="Claude Sonnet 4.6"),
        MagicMock(id="claude-haiku-4-5", display_name="Claude Haiku 4.5"),
    ]
    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value.models.list.return_value = _make_fake_sdk_page(fake_models)

    with patch("wisper_transcribe.web.routes.config.get_llm_api_key", return_value="sk-ant-test"), \
         patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        resp = client.post("/config/anthropic-models")

    assert resp.status_code == 200
    data = resp.json()
    assert data["running"] is True
    assert {m["name"] for m in data["models"]} == {"claude-sonnet-4-6", "claude-haiku-4-5"}
    assert data["models"][0]["size"] in {"Claude Sonnet 4.6", "Claude Haiku 4.5"}


def test_anthropic_models_form_key_takes_precedence(client):
    """A non-empty api_key in the form body overrides env/config resolution."""
    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value.models.list.return_value = _make_fake_sdk_page([])

    with patch("wisper_transcribe.web.routes.config.get_llm_api_key",
               return_value="should-not-be-used") as resolver, \
         patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        resp = client.post("/config/anthropic-models", data={"api_key": "sk-ant-from-form"})

    assert resp.status_code == 200
    # SDK was called with the form-supplied key, not the resolver's value
    fake_anthropic.Anthropic.assert_called_once_with(api_key="sk-ant-from-form")
    # Resolver is never consulted when form supplies a key
    resolver.assert_not_called()


def test_anthropic_models_api_error_returns_running_false(client):
    """An exception from the SDK is reported as running=False with a generic message."""
    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value.models.list.side_effect = RuntimeError("401")

    with patch("wisper_transcribe.web.routes.config.get_llm_api_key", return_value="bad-key"), \
         patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        resp = client.post("/config/anthropic-models")

    data = resp.json()
    assert data["running"] is False
    assert "Anthropic API call failed" in data["error"]
    # Error message must not leak the underlying exception text (e.g. could include key fragments)
    assert "401" not in data["error"]


def test_openai_models_filters_to_chat_only(client):
    """Whisper, dall-e, embedding, and instruct variants are filtered out."""
    fake_models = [
        MagicMock(id="gpt-4o-mini"),
        MagicMock(id="gpt-4-turbo"),
        MagicMock(id="o3-mini"),
        MagicMock(id="o1"),
        MagicMock(id="chatgpt-4o-latest"),
        MagicMock(id="whisper-1"),               # excluded
        MagicMock(id="dall-e-3"),                # excluded
        MagicMock(id="text-embedding-3-large"),  # excluded
        MagicMock(id="gpt-3.5-turbo-instruct"),  # excluded (instruct)
        MagicMock(id="gpt-4o-audio-preview"),    # excluded (audio)
        MagicMock(id="omni-moderation-latest"),  # excluded (moderation)
        MagicMock(id="babbage-002"),             # excluded (babbage)
    ]
    fake_openai = MagicMock()
    fake_openai.OpenAI.return_value.models.list.return_value = _make_fake_sdk_page(fake_models)

    with patch("wisper_transcribe.web.routes.config.get_llm_api_key", return_value="sk-test"), \
         patch.dict("sys.modules", {"openai": fake_openai}):
        resp = client.post("/config/openai-models")

    data = resp.json()
    assert data["running"] is True
    names = {m["name"] for m in data["models"]}
    assert names == {"gpt-4o-mini", "gpt-4-turbo", "o3-mini", "o1", "chatgpt-4o-latest"}


def test_google_models_filters_to_gemini_chat(client):
    """Embedding, AQA, and Imagen variants are filtered out; 'models/' prefix is stripped."""
    fake_models = [
        MagicMock(name="m1"),
        MagicMock(name="m2"),
        MagicMock(name="m3"),
        MagicMock(name="m4"),
        MagicMock(name="m5"),
    ]
    # MagicMock interprets `name=` as the mock's display name, so set the attribute manually
    fake_models[0].name = "models/gemini-2.0-flash"
    fake_models[1].name = "models/gemini-1.5-pro"
    fake_models[2].name = "models/text-embedding-004"    # excluded
    fake_models[3].name = "models/aqa"                   # excluded
    fake_models[4].name = "models/imagen-3.0-generate"   # excluded

    fake_genai = MagicMock()
    fake_genai.Client.return_value.models.list.return_value = iter(fake_models)

    with patch("wisper_transcribe.web.routes.config.get_llm_api_key", return_value="AIza-test"), \
         patch.dict("sys.modules", {"google.genai": fake_genai, "google": MagicMock(genai=fake_genai)}):
        resp = client.post("/config/google-models")

    data = resp.json()
    assert data["running"] is True
    names = {m["name"] for m in data["models"]}
    assert names == {"gemini-2.0-flash", "gemini-1.5-pro"}


def test_openai_models_sdk_missing(client):
    """SDK not installed returns a helpful install hint."""
    import sys
    with patch("wisper_transcribe.web.routes.config.get_llm_api_key", return_value="sk-test"), \
         patch.dict(sys.modules, {"openai": None}):
        resp = client.post("/config/openai-models")
    data = resp.json()
    assert data["running"] is False
    assert "wisper-transcribe[llm-openai]" in data["error"]


def test_anthropic_models_api_key_not_in_url(client):
    """The key must never appear in a URL — POST endpoints only."""
    # Sanity: GET should 405
    resp = client.get("/config/anthropic-models")
    assert resp.status_code == 405


def test_anthropic_models_oversized_key_rejected(client):
    """A multi-kilobyte api_key in the form is ignored (falls back to env/config)."""
    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value.models.list.return_value = _make_fake_sdk_page([])

    with patch("wisper_transcribe.web.routes.config.get_llm_api_key",
               return_value="sk-ant-config") as resolver, \
         patch.dict("sys.modules", {"anthropic": fake_anthropic}):
        resp = client.post("/config/anthropic-models",
                           data={"api_key": "x" * 5000})

    assert resp.status_code == 200
    # Oversized form key was rejected → resolver was consulted, returning the config value
    resolver.assert_called_once_with("anthropic")
    fake_anthropic.Anthropic.assert_called_once_with(api_key="sk-ant-config")


def test_preset_add_valid_saves_preset(client):
    """POST /config/presets/add with valid snowflake IDs appends a preset and redirects."""
    captured = {}

    def fake_save(cfg):
        captured.update(cfg)

    with patch("wisper_transcribe.web.routes.config.load_config",
               return_value={"discord_presets": []}), \
         patch("wisper_transcribe.web.routes.config.save_config", side_effect=fake_save):
        resp = client.post(
            "/config/presets/add",
            data={
                "name": "Weekly D&D",
                "guild_id": "123456789012345678",
                "channel_id": "876543210987654321",
            },
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert "preset_saved=1" in resp.headers["location"]
    presets = captured.get("discord_presets", [])
    assert any(p["name"] == "Weekly D&D" for p in presets)
    assert any(p["guild_id"] == "123456789012345678" for p in presets)


def test_preset_add_invalid_snowflake_rejected(client):
    """POST /config/presets/add rejects a non-numeric guild_id and does not save."""
    with patch("wisper_transcribe.web.routes.config.load_config", return_value={}), \
         patch("wisper_transcribe.web.routes.config.save_config") as mock_save:
        resp = client.post(
            "/config/presets/add",
            data={
                "name": "My Game",
                "guild_id": "not-a-snowflake",
                "channel_id": "123456789012345678",
            },
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert "preset_error=invalid" in resp.headers["location"]
    mock_save.assert_not_called()


def test_preset_add_missing_name_rejected(client):
    """POST /config/presets/add rejects an empty preset name and does not save."""
    with patch("wisper_transcribe.web.routes.config.load_config", return_value={}), \
         patch("wisper_transcribe.web.routes.config.save_config") as mock_save:
        resp = client.post(
            "/config/presets/add",
            data={
                "name": "",
                "guild_id": "123456789012345678",
                "channel_id": "876543210987654321",
            },
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert "preset_error=invalid" in resp.headers["location"]
    mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# Speakers
# ---------------------------------------------------------------------------


def test_speakers_list_returns_200(client):
    with patch("wisper_transcribe.web.routes.speakers.load_profiles", return_value={}):
        resp = client.get("/speakers")
    assert resp.status_code == 200


def test_speakers_enroll_form_returns_200(client):
    resp = client.get("/speakers/enroll")
    assert resp.status_code == 200


def test_speakers_remove_redirects(client, tmp_path):
    from wisper_transcribe.models import SpeakerProfile
    fake_emb = tmp_path / "alice.npy"
    fake_emb.write_bytes(b"")
    fake_profile = SpeakerProfile(
        name="alice",
        display_name="Alice",
        role="DM",
        embedding_path=fake_emb,
        enrolled_date="2026-04-07",
        enrollment_source="test.mp3",
    )
    profiles = {"alice": fake_profile}

    with patch("wisper_transcribe.web.routes.speakers.load_profiles", return_value=profiles), \
         patch("wisper_transcribe.web.routes.speakers.save_profiles") as mock_save:
        resp = client.post("/speakers/alice/remove", follow_redirects=False)

    assert resp.status_code == 303
    mock_save.assert_called_once_with({})  # alice removed, empty dict saved


def test_speakers_rename_updates_display_name(client):
    from wisper_transcribe.models import SpeakerProfile
    fake_profile = SpeakerProfile(
        name="alice",
        display_name="Alice",
        role="DM",
        embedding_path=Path("/tmp/alice.npy"),
        enrolled_date="2026-04-07",
        enrollment_source="test.mp3",
    )
    profiles = {"alice": fake_profile}

    with patch("wisper_transcribe.web.routes.speakers.load_profiles", return_value=profiles), \
         patch("wisper_transcribe.web.routes.speakers.save_profiles"):
        resp = client.post(
            "/speakers/alice/rename",
            data={"new_name": "Alice (DM)"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert profiles["alice"].display_name == "Alice (DM)"


# ---------------------------------------------------------------------------
# LLM post-processing routes (refine / summarize / summary view)
# ---------------------------------------------------------------------------

_TRANSCRIPT_MD = (
    "---\ntitle: Session 01\ndate_processed: '2026-04-07'\n"
    "speakers:\n  - name: Alice\n    role: DM\n---\n\n"
    "**Alice** *(00:00)*: Hello, world."
)

_SUMMARY_MD = (
    "---\ntype: session-summary\nsource: \"session01.md\"\n"
    "generated_at: '2026-04-07T12:00:00'\nprovider: ollama\n"
    "model: \"llama3.1:8b\"\nrefined: false\n---\n\n"
    "# Session 01\n\n## Summary\n\nThe party gathered.\n"
)


def test_transcripts_list_excludes_summary_files(client, tmp_path):
    """Summary sidecars must not appear as independent cards in the transcript list."""
    (tmp_path / "session01.md").write_text(_TRANSCRIPT_MD)
    (tmp_path / "session01.summary.md").write_text(_SUMMARY_MD)
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.get("/transcripts")
    assert resp.status_code == 200
    # The main transcript card should appear once
    assert resp.content.count(b"session01") >= 1
    # The summary card should NOT appear as a separate entry
    # (the summary is shown as an icon within the transcript card, not its own card)
    html = resp.content.decode()
    assert "session01.summary" not in html or html.count("session01.summary") <= 2  # href only


def test_transcript_detail_shows_summary_link(client, tmp_path):
    """When a .summary.md sidecar exists, the detail page shows the Campaign Notes panel."""
    (tmp_path / "session01.md").write_text(_TRANSCRIPT_MD)
    (tmp_path / "session01.summary.md").write_text(_SUMMARY_MD)
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.get("/transcripts/session01")
    assert resp.status_code == 200
    assert b"Campaign Notes" in resp.content


def test_transcript_detail_no_summary_link_when_absent(client, tmp_path):
    """Without a sidecar the Campaign Notes panel must not appear."""
    (tmp_path / "session01.md").write_text(_TRANSCRIPT_MD)
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.get("/transcripts/session01")
    assert resp.status_code == 200
    assert b"Campaign Notes available" not in resp.content


@pytest.mark.filterwarnings("ignore:fix_vocabulary:UserWarning")
def test_post_refine_queues_job_and_redirects(client, tmp_path):
    """POST /transcripts/<name>/refine submits an LLM job and redirects."""
    (tmp_path / "session01.md").write_text(_TRANSCRIPT_MD)
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post("/transcripts/session01/refine", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/transcribe/jobs/")


@pytest.mark.filterwarnings("ignore:fix_vocabulary:UserWarning")
def test_post_summarize_queues_job_and_redirects(client, tmp_path):
    """POST /transcripts/<name>/summarize submits an LLM job and redirects."""
    (tmp_path / "session01.md").write_text(_TRANSCRIPT_MD)
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post("/transcripts/session01/summarize", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/transcribe/jobs/")


def test_post_refine_nonexistent_transcript_404(client, tmp_path):
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post("/transcripts/nonexistent/refine", follow_redirects=False)
    assert resp.status_code == 404


def test_summary_detail_renders(client, tmp_path):
    """GET /transcripts/<name>/summary renders the summary markdown."""
    (tmp_path / "session01.md").write_text(_TRANSCRIPT_MD)
    (tmp_path / "session01.summary.md").write_text(_SUMMARY_MD)
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.get("/transcripts/session01/summary")
    assert resp.status_code == 200
    assert b"Session 01" in resp.content
    assert b"Transcript" in resp.content  # back-link


def test_summary_detail_not_found(client, tmp_path):
    (tmp_path / "session01.md").write_text(_TRANSCRIPT_MD)
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.get("/transcripts/session01/summary")
    assert resp.status_code == 404


def test_summary_download(client, tmp_path):
    """GET /transcripts/<name>/summary/download serves the .summary.md file."""
    (tmp_path / "session01.md").write_text(_TRANSCRIPT_MD)
    (tmp_path / "session01.summary.md").write_text(_SUMMARY_MD)
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.get("/transcripts/session01/summary/download")
    assert resp.status_code == 200
    assert b"session-summary" in resp.content


def test_delete_transcript_also_removes_summary(client, tmp_path):
    """Deleting a transcript removes the .summary.md sidecar too."""
    md = tmp_path / "session01.md"
    sm = tmp_path / "session01.summary.md"
    md.write_text(_TRANSCRIPT_MD)
    sm.write_text(_SUMMARY_MD)
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post("/transcripts/session01/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert not md.exists()
    assert not sm.exists()


def test_transcribe_post_accepts_post_processing_flags(client, tmp_path):
    """post_refine and post_summarize checkboxes must not cause a 422."""
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"fake mp3")
    with open(audio_file, "rb") as f:
        resp = client.post(
            "/transcribe",
            files={"file": ("test.mp3", f, "audio/mpeg")},
            data={"post_refine": "1", "post_summarize": "1"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/transcribe/jobs/")


def test_transcribe_post_with_vocab_file_sets_hotwords(client, tmp_path, monkeypatch):
    """Uploading a .txt vocab file parses and passes hotwords to the job queue."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.web.jobs import Job
    import uuid

    fake_job = MagicMock(spec=Job)
    fake_job.id = str(uuid.uuid4())

    vocab_content = b"# comment line\nAragorn\nGandalf\n\n  Frodo  \n"

    with patch("wisper_transcribe.web.routes.transcribe.get_output_dir", return_value=tmp_path), \
         patch.object(client.app.state.job_queue, "submit", return_value=fake_job) as mock_submit:
        resp = client.post(
            "/transcribe",
            files={
                "file": ("session.mp3", b"fake audio", "audio/mpeg"),
                "vocab_file": ("vocab.txt", vocab_content, "text/plain"),
            },
            follow_redirects=False,
        )

    assert resp.status_code == 303
    call_kwargs = mock_submit.call_args.kwargs
    assert call_kwargs.get("hotwords") == ["Aragorn", "Gandalf", "Frodo"]


def test_transcribe_post_no_vocab_file_hotwords_is_none(client, tmp_path, monkeypatch):
    """Without a vocab file, hotwords is None (falls back to config defaults)."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.web.jobs import Job
    import uuid

    fake_job = MagicMock(spec=Job)
    fake_job.id = str(uuid.uuid4())

    with patch("wisper_transcribe.web.routes.transcribe.get_output_dir", return_value=tmp_path), \
         patch.object(client.app.state.job_queue, "submit", return_value=fake_job) as mock_submit:
        resp = client.post(
            "/transcribe",
            files={"file": ("session.mp3", b"fake", "audio/mpeg")},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    call_kwargs = mock_submit.call_args.kwargs
    assert call_kwargs.get("hotwords") is None


def test_speakers_rename_empty_name_no_change(client):
    from wisper_transcribe.models import SpeakerProfile
    fake_profile = SpeakerProfile(
        name="alice",
        display_name="Alice",
        role="DM",
        embedding_path=Path("/tmp/alice.npy"),
        enrolled_date="2026-04-07",
        enrollment_source="test.mp3",
    )
    profiles = {"alice": fake_profile}

    with patch("wisper_transcribe.web.routes.speakers.load_profiles", return_value=profiles), \
         patch("wisper_transcribe.web.routes.speakers.save_profiles") as mock_save:
        resp = client.post(
            "/speakers/alice/rename",
            data={"new_name": ""},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    mock_save.assert_not_called()  # empty name: no save
    assert profiles["alice"].display_name == "Alice"  # unchanged


# ---------------------------------------------------------------------------
# Campaigns routes
# ---------------------------------------------------------------------------

def test_campaigns_index_empty(client, tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    with patch("wisper_transcribe.web.routes.campaigns.load_campaigns", return_value={}), \
         patch("wisper_transcribe.web.routes.campaigns.load_profiles", return_value={}):
        resp = client.get("/campaigns")
    assert resp.status_code == 200
    assert "No campaigns" in resp.text


def test_campaigns_create_and_redirect(client, tmp_path, monkeypatch):
    """POST /campaigns creates a campaign and redirects via the server-generated slug."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.models import Campaign
    created_campaign = Campaign(slug="dnd-mondays", display_name="D&D Mondays",
                                created="2026-04-28", members={})

    with patch("wisper_transcribe.web.routes.campaigns.create_campaign",
               return_value=created_campaign) as mock_create:
        resp = client.post(
            "/campaigns",
            data={"display_name": "D&D Mondays"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    location = resp.headers.get("location", "")
    # Location must be the server-generated slug, not arbitrary user input
    assert "/campaigns/dnd-mondays" in location
    mock_create.assert_called_once_with("D&D Mondays")


def test_campaigns_create_empty_name_rejected(client, tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    resp = client.post(
        "/campaigns",
        data={"display_name": "  "},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "error=invalid_name" in resp.headers.get("location", "")


def test_campaign_detail_unknown_slug(client, tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    with patch("wisper_transcribe.web.routes.campaigns.load_campaigns", return_value={}):
        resp = client.get("/campaigns/does-not-exist", follow_redirects=False)
    # Redirect with error, not a traceback
    assert resp.status_code == 303
    assert "error=not_found" in resp.headers.get("location", "")


def test_campaign_add_member_persists(client, tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.models import SpeakerProfile, Campaign, CampaignMember
    import numpy as np

    # Create real campaign + profile
    (tmp_path / "profiles" / "embeddings").mkdir(parents=True)
    fake_emb = tmp_path / "profiles" / "embeddings" / "alice.npy"
    np.save(str(fake_emb), np.zeros(512))

    from wisper_transcribe.campaign_manager import create_campaign
    from wisper_transcribe.speaker_manager import save_profiles
    create_campaign("Test Game", data_dir=tmp_path)
    save_profiles(
        {"alice": SpeakerProfile(
            name="alice", display_name="Alice", role="",
            embedding_path=fake_emb, enrolled_date="2026-04-28",
            enrollment_source="test.mp3",
        )},
        data_dir=tmp_path,
    )

    resp = client.post(
        "/campaigns/test-game/members",
        data={"profile_key": "alice", "role": "DM", "character": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/campaigns/test-game" in resp.headers.get("location", "")

    from wisper_transcribe.campaign_manager import load_campaigns
    loaded = load_campaigns(tmp_path)
    assert "alice" in loaded["test-game"].members
    assert loaded["test-game"].members["alice"].role == "DM"


def test_campaign_add_unknown_profile_rejects(client, tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.campaign_manager import create_campaign
    create_campaign("Test Game", data_dir=tmp_path)

    with patch("wisper_transcribe.web.routes.campaigns.load_profiles", return_value={}):
        resp = client.post(
            "/campaigns/test-game/members",
            data={"profile_key": "nobody", "role": "", "character": ""},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert "error=unknown_profile" in resp.headers.get("location", "")


def test_campaign_remove_member_does_not_delete_profile(client, tmp_path, monkeypatch):
    """Removing a member from a campaign never deletes the profile or embedding."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    import numpy as np
    from wisper_transcribe.models import SpeakerProfile
    from wisper_transcribe.campaign_manager import create_campaign, add_member
    from wisper_transcribe.speaker_manager import save_profiles

    (tmp_path / "profiles" / "embeddings").mkdir(parents=True)
    fake_emb = tmp_path / "profiles" / "embeddings" / "alice.npy"
    np.save(str(fake_emb), np.zeros(512))
    save_profiles(
        {"alice": SpeakerProfile(
            name="alice", display_name="Alice", role="",
            embedding_path=fake_emb, enrolled_date="2026-04-28",
            enrollment_source="test.mp3",
        )},
        data_dir=tmp_path,
    )
    create_campaign("Test Game", data_dir=tmp_path)
    add_member("test-game", "alice", data_dir=tmp_path)

    resp = client.post("/campaigns/test-game/members/alice/remove", follow_redirects=False)
    assert resp.status_code == 303
    # Profile and embedding must still exist
    assert fake_emb.exists()
    from wisper_transcribe.campaign_manager import get_campaign_profile_keys
    assert "alice" not in get_campaign_profile_keys("test-game", data_dir=tmp_path)


def test_transcribe_form_includes_campaign_select(client, tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.models import Campaign
    campaigns = {"dnd-mondays": Campaign(slug="dnd-mondays", display_name="D&D Mondays",
                                        created="2026-04-28", members={})}
    with patch("wisper_transcribe.web.routes.transcribe.load_campaigns", return_value=campaigns):
        resp = client.get("/transcribe")
    assert resp.status_code == 200
    assert 'name="campaign"' in resp.text
    assert "D&amp;D Mondays" in resp.text or "D&D Mondays" in resp.text


def test_transcribe_post_with_campaign_passes_to_queue(client, tmp_path, monkeypatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.web.jobs import Job
    import uuid

    fake_job = MagicMock(spec=Job)
    fake_job.id = str(uuid.uuid4())

    with patch("wisper_transcribe.web.routes.transcribe.load_campaigns", return_value={}), \
         patch("wisper_transcribe.web.routes.transcribe.get_output_dir",
               return_value=tmp_path), \
         patch.object(
             client.app.state.job_queue, "submit", return_value=fake_job
         ) as mock_submit:
        resp = client.post(
            "/transcribe",
            files={"file": ("session.mp3", b"fake", "audio/mpeg")},
            data={"campaign": "dnd-mondays"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    call_kwargs = mock_submit.call_args.kwargs
    assert call_kwargs.get("campaign") == "dnd-mondays"


# ---------------------------------------------------------------------------
# Transcript campaign grouping
# ---------------------------------------------------------------------------


def test_transcripts_list_groups_by_campaign(client, tmp_path, monkeypatch):
    """Transcripts belonging to a campaign appear under its folder section."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    (tmp_path / "session01.md").write_text("---\ntitle: Session 01\n---\n")
    from wisper_transcribe.models import Campaign
    campaigns = {"alpha": Campaign(slug="alpha", display_name="Alpha Game",
                                  created="2026-04-28", members={},
                                  transcripts=["session01"])}
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path), \
         patch("wisper_transcribe.web.routes.transcripts.load_campaigns", return_value=campaigns):
        resp = client.get("/transcripts")
    assert resp.status_code == 200
    assert "Alpha Game" in resp.text
    assert "session01" in resp.text


def test_transcripts_list_shows_uncampaigned(client, tmp_path, monkeypatch):
    """Transcripts with no campaign appear in the ungrouped section."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    (tmp_path / "orphan.md").write_text("---\ntitle: Orphan\n---\n")
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path), \
         patch("wisper_transcribe.web.routes.transcripts.load_campaigns", return_value={}):
        resp = client.get("/transcripts")
    assert resp.status_code == 200
    assert "orphan" in resp.text


def test_transcript_detail_shows_campaign_dropdown(client, tmp_path, monkeypatch):
    """Transcript detail page shows the campaign assignment dropdown when campaigns exist."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    md = tmp_path / "session01.md"
    md.write_text("---\ntitle: Session 01\n---\n\nHello.")
    from wisper_transcribe.models import Campaign
    campaigns = {"alpha": Campaign(slug="alpha", display_name="Alpha Game",
                                  created="2026-04-28", members={}, transcripts=[])}
    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path), \
         patch("wisper_transcribe.web.routes.transcripts.load_campaigns", return_value=campaigns), \
         patch("wisper_transcribe.web.routes.transcripts.get_campaign_for_transcript",
               return_value=None):
        resp = client.get("/transcripts/session01")
    assert resp.status_code == 200
    assert 'name="campaign"' in resp.text
    assert "Alpha Game" in resp.text


def test_assign_campaign_moves_transcript(client, tmp_path, monkeypatch):
    """POST /transcripts/{name}/campaign persists the assignment."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    md = tmp_path / "session01.md"
    md.write_text("---\ntitle: Session 01\n---\n")
    from wisper_transcribe.campaign_manager import create_campaign, get_campaign_for_transcript
    create_campaign("Test Game", data_dir=tmp_path)

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post(
            "/transcripts/session01/campaign",
            data={"campaign": "test-game"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert get_campaign_for_transcript("session01", data_dir=tmp_path) == "test-game"


def test_assign_campaign_unlinks_when_empty(client, tmp_path, monkeypatch):
    """POST with empty campaign field removes the association."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    md = tmp_path / "session01.md"
    md.write_text("---\ntitle: Session 01\n---\n")
    from wisper_transcribe.campaign_manager import (
        create_campaign, move_transcript_to_campaign, get_campaign_for_transcript
    )
    create_campaign("Test Game", data_dir=tmp_path)
    move_transcript_to_campaign("session01", "test-game", data_dir=tmp_path)

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post(
            "/transcripts/session01/campaign",
            data={"campaign": ""},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert get_campaign_for_transcript("session01", data_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# Enrollment bug fix — enroll_submit must call enroll_speaker()
# ---------------------------------------------------------------------------


def test_enroll_submit_calls_enroll_speaker_when_segments_present(
    client, tmp_path, monkeypatch
):
    """enroll_submit() must call enroll_speaker() for each rename when diarization_segments exist."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

    # Set up a completed job with diarization_segments
    from wisper_transcribe.web.jobs import Job, COMPLETED
    from datetime import datetime
    import uuid

    transcript = tmp_path / "session01.md"
    transcript.write_text(
        "---\nspeakers:\n  - name: SPEAKER_00\n---\n\n**SPEAKER_00** *(00:00)*: Hello."
    )
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake")

    fake_segment = MagicMock()
    fake_segment.speaker = "SPEAKER_00"

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(audio),
        kwargs={"device": "cpu"},
        output_path=str(transcript),
        diarization_segments=[fake_segment],
    )
    client.app.state.job_queue._jobs[job.id] = job

    with patch("wisper_transcribe.speaker_manager.enroll_speaker") as mock_enroll:
        resp = client.post(
            f"/transcribe/jobs/{job.id}/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    mock_enroll.assert_called_once()
    call_kwargs = mock_enroll.call_args.kwargs
    assert call_kwargs["display_name"] == "Alice"
    assert call_kwargs["name"] == "alice"
    assert call_kwargs["speaker_label"] == "SPEAKER_00"


def test_enroll_submit_adds_speaker_to_job_campaign(client, tmp_path, monkeypatch):
    """When the job was submitted with a campaign, enrolled speakers must be added to it."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

    from wisper_transcribe.web.jobs import Job, COMPLETED
    from wisper_transcribe.campaign_manager import create_campaign, get_campaign_profile_keys
    from datetime import datetime
    import uuid

    # Pre-create the campaign that the job references
    create_campaign("D&D Mondays")

    transcript = tmp_path / "session01.md"
    transcript.write_text(
        "---\nspeakers:\n  - name: SPEAKER_00\n---\n\n**SPEAKER_00** *(00:00)*: Hi."
    )
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake")
    fake_segment = MagicMock()
    fake_segment.speaker = "SPEAKER_00"

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(audio),
        kwargs={"device": "cpu", "campaign": "d-d-mondays"},
        output_path=str(transcript),
        diarization_segments=[fake_segment],
    )
    client.app.state.job_queue._jobs[job.id] = job

    with patch("wisper_transcribe.speaker_manager.enroll_speaker"):
        resp = client.post(
            f"/transcribe/jobs/{job.id}/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    # The enrolled profile must now be a member of the campaign
    assert "alice" in get_campaign_profile_keys("d-d-mondays")


def test_enroll_submit_no_campaign_skips_add_member(client, tmp_path, monkeypatch):
    """No campaign on the job → add_member is never called (normal enrollment path)."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

    from wisper_transcribe.web.jobs import Job, COMPLETED
    from datetime import datetime
    import uuid

    transcript = tmp_path / "session01.md"
    transcript.write_text(
        "---\nspeakers:\n  - name: SPEAKER_00\n---\n\n**SPEAKER_00** *(00:00)*: Hi."
    )
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake")
    fake_segment = MagicMock()
    fake_segment.speaker = "SPEAKER_00"

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(audio),
        kwargs={"device": "cpu"},  # no campaign
        output_path=str(transcript),
        diarization_segments=[fake_segment],
    )
    client.app.state.job_queue._jobs[job.id] = job

    with patch("wisper_transcribe.speaker_manager.enroll_speaker"), \
         patch("wisper_transcribe.campaign_manager.add_member") as mock_add_member:
        resp = client.post(
            f"/transcribe/jobs/{job.id}/enroll",
            data={"speaker_SPEAKER_00": "Bob"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    mock_add_member.assert_not_called()


def test_enroll_submit_skips_add_member_if_already_in_campaign(client, tmp_path, monkeypatch):
    """If the profile is already in the campaign, add_member must not be called (avoids clobbering role/character)."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

    from wisper_transcribe.web.jobs import Job, COMPLETED
    from wisper_transcribe.campaign_manager import create_campaign, add_member
    from datetime import datetime
    import uuid

    create_campaign("D&D Mondays")
    add_member("d-d-mondays", "alice", role="player", character="Tika")

    transcript = tmp_path / "session01.md"
    transcript.write_text(
        "---\nspeakers:\n  - name: SPEAKER_00\n---\n\n**SPEAKER_00** *(00:00)*: Hi."
    )
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake")
    fake_segment = MagicMock()
    fake_segment.speaker = "SPEAKER_00"

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(audio),
        kwargs={"device": "cpu", "campaign": "d-d-mondays"},
        output_path=str(transcript),
        diarization_segments=[fake_segment],
    )
    client.app.state.job_queue._jobs[job.id] = job

    with patch("wisper_transcribe.speaker_manager.enroll_speaker"), \
         patch("wisper_transcribe.campaign_manager.add_member") as mock_add_member:
        resp = client.post(
            f"/transcribe/jobs/{job.id}/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    # add_member must not be invoked a second time — Alice is already in the roster
    mock_add_member.assert_not_called()


def test_enroll_submit_skips_enroll_when_no_segments(client, tmp_path, monkeypatch):
    """enroll_submit() must not call enroll_speaker() when no diarization_segments are stored."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

    from wisper_transcribe.web.jobs import Job, COMPLETED
    from datetime import datetime
    import uuid

    transcript = tmp_path / "session01.md"
    transcript.write_text("---\nspeakers:\n  - name: SPEAKER_00\n---\n\nHello.")
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake")

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(audio),
        kwargs={"device": "cpu"},
        output_path=str(transcript),
        diarization_segments=[],  # no segments
    )
    client.app.state.job_queue._jobs[job.id] = job

    with patch("wisper_transcribe.speaker_manager.enroll_speaker") as mock_enroll:
        resp = client.post(
            f"/transcribe/jobs/{job.id}/enroll",
            data={"speaker_SPEAKER_00": "Bob"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    mock_enroll.assert_not_called()


# ---------------------------------------------------------------------------
# Bulk transcript operations (E3)
# ---------------------------------------------------------------------------


def test_bulk_delete_removes_multiple_transcripts(client, tmp_path, monkeypatch):
    """POST /transcripts/bulk-delete deletes all listed stems and redirects."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    (tmp_path / "session01.md").write_text("# S1")
    (tmp_path / "session02.md").write_text("# S2")
    (tmp_path / "session02.summary.md").write_text("# notes")

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post(
            "/transcripts/bulk-delete",
            data={"stems": ["session01", "session02"]},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/transcripts"
    assert not (tmp_path / "session01.md").exists()
    assert not (tmp_path / "session02.md").exists()
    assert not (tmp_path / "session02.summary.md").exists()


def test_bulk_delete_skips_invalid_stems(client, tmp_path, monkeypatch):
    """bulk-delete silently skips stems that fail path validation."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    (tmp_path / "good.md").write_text("# ok")

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post(
            "/transcripts/bulk-delete",
            data={"stems": ["good", "../../etc/passwd", "\x00bad"]},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert not (tmp_path / "good.md").exists()  # valid stem deleted


def test_bulk_campaign_assigns_multiple_transcripts(client, tmp_path, monkeypatch):
    """POST /transcripts/bulk-campaign calls move_transcript_to_campaign for each stem."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    (tmp_path / "s1.md").write_text("# S1")
    (tmp_path / "s2.md").write_text("# S2")

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path), \
         patch("wisper_transcribe.web.routes.transcripts.move_transcript_to_campaign") as mock_move:
        resp = client.post(
            "/transcripts/bulk-campaign",
            data={"stems": ["s1", "s2"], "campaign": "my-campaign"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert mock_move.call_count == 2
    calls = {c.args[0] for c in mock_move.call_args_list}
    assert calls == {"s1", "s2"}


def test_bulk_campaign_invalid_slug_redirects_with_error(client, tmp_path, monkeypatch):
    """bulk-campaign rejects an invalid campaign slug."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post(
            "/transcripts/bulk-campaign",
            data={"stems": ["s1"], "campaign": "../../evil"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert "error=invalid_campaign" in resp.headers["location"]
