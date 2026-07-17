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
         patch("wisper_transcribe.web.routes.dashboard.get_data_dir", return_value=str(tmp_path)), \
         patch("wisper_transcribe.web.routes.dashboard.get_output_dir", return_value=tmp_path / "output"):
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
         patch("wisper_transcribe.web.routes.dashboard.get_data_dir", return_value=str(tmp_path)), \
         patch("wisper_transcribe.web.routes.dashboard.get_output_dir", return_value=tmp_path / "output"):
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
         patch("wisper_transcribe.web.routes.dashboard.get_data_dir", return_value=str(tmp_path)), \
         patch("wisper_transcribe.web.routes.dashboard.get_output_dir", return_value=tmp_path / "output"):
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "anthropic" in body
    assert "API key missing" in body
    # Resolved default model is shown when llm_model is blank
    assert "claude-sonnet-5" in body


def test_dashboard_transcript_count_excludes_summaries(client, tmp_path):
    """R21: the dashboard's transcript count must exclude .summary.md sidecars,
    same as the Transcripts page — they are LLM-generated notes, not transcripts."""
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    (out_dir / "session01.md").write_text("# transcript")
    (out_dir / "session01.summary.md").write_text("# summary")
    (out_dir / "session02.md").write_text("# transcript 2")

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.web.routes.dashboard.load_config", return_value={}), \
         patch("wisper_transcribe.web.routes.dashboard.get_device", return_value="cpu"), \
         patch("wisper_transcribe.web.routes.dashboard.get_output_dir", return_value=out_dir):
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.content.decode()
    # Two real transcripts, not three — the summary sidecar must not be counted.
    assert "2 transcripts in the archive" in body


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


def test_transcribe_post_streams_large_upload_correctly(client, tmp_path):
    """R10: uploads are streamed to disk in 1 MiB chunks rather than
    buffered whole into memory. A payload spanning multiple chunks must
    still land on disk byte-for-byte before the job is submitted."""
    payload = b"A" * (1 << 20) + b"B" * (1 << 19)  # 1.5 MiB, crosses a chunk boundary

    captured: dict = {}

    class _FakeJob:
        id = "fake-job-id"

    def _fake_submit(input_path, **kwargs):
        captured["content"] = Path(input_path).read_bytes()
        return _FakeJob()

    with patch.object(client.app.state.job_queue, "submit", side_effect=_fake_submit):
        resp = client.post(
            "/transcribe",
            files={"file": ("test.mp3", payload, "audio/mpeg")},
            data={},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert captured.get("content") == payload


def test_job_stream_serves_lines_after_log_trimming(client, tmp_path):
    """R14: once Job.log_lines has been trimmed (log_lines_dropped > 0), the
    SSE stream must still serve the retained lines using the translated
    index instead of crashing or resending stale data."""
    from wisper_transcribe.web.jobs import Job, COMPLETED
    from datetime import datetime

    job = Job(
        id="trim-test-job",
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(tmp_path / "audio.mp3"),
        kwargs={},
        finished_at=datetime.now(),
    )
    # Simulate a job that appended far more than _MAX_LOG_LINES: only the
    # tail is retained, and log_lines_dropped records how much was trimmed.
    job.log_lines = ["line 998", "line 999"]
    job.log_lines_dropped = 998

    client.app.state.job_queue._jobs[job.id] = job

    with client.stream("GET", f"/transcribe/jobs/{job.id}/stream") as resp:
        body = "".join(resp.iter_text())

    assert '"line 998"' in body
    assert '"line 999"' in body
    assert '"status": "completed"' in body


def test_transcribe_post_empty_upload_still_queues(client, tmp_path):
    """An empty upload is still accepted at the route layer (the chunked
    read loop must not choke on an immediate EOF) -- validation of the
    resulting empty file happens downstream in the job itself, unchanged
    by the R10 streaming fix."""
    resp = client.post(
        "/transcribe",
        files={"file": ("empty.mp3", b"", "audio/mpeg")},
        data={},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/transcribe/jobs/")


@pytest.mark.parametrize("field,bad_value", [
    ("model_size", "not-a-real-model"),
    ("device", "quantum"),
    ("compute_type", "bfloat9000"),
])
def test_transcribe_post_invalid_enum_redirects_with_generic_error(client, tmp_path, field, bad_value):
    """R33: model_size/device/compute_type are validated against the same
    canonical enums the CLI uses. An invalid value redirects to /transcribe
    with a generic error code — never the raw value (CLAUDE.md: never echo
    user input into a redirect)."""
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"fake mp3")

    with open(audio_file, "rb") as f:
        resp = client.post(
            "/transcribe",
            files={"file": ("test.mp3", f, "audio/mpeg")},
            data={field: bad_value},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/transcribe?error=invalid_option"
    assert bad_value not in resp.headers["location"]


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


@pytest.mark.parametrize("platform,expected_label", [
    ("darwin", "Open in Finder"),
    ("win32", "Open in Explorer"),
    ("linux", "Show in Files"),
])
def test_config_get_uses_os_specific_open_label(client, platform, expected_label):
    """The data-dir button verb must match the host OS file manager."""
    with patch("wisper_transcribe.web.routes.config.load_config", return_value={}), \
         patch("wisper_transcribe.web.routes.config.get_config_path", return_value=Path("/tmp/config.toml")), \
         patch("wisper_transcribe.web.routes.config.sys.platform", platform):
        resp = client.get("/config")
    assert resp.status_code == 200
    assert expected_label.encode() in resp.content


def test_config_open_data_dir_get_is_rejected(client):
    """R16: open-data-dir spawns an OS process (state-changing), so the GET
    verb — triggerable cross-site via <img src=...> — must not be routed."""
    with patch("subprocess.Popen") as mock_popen:
        resp = client.get("/config/open-data-dir")
    assert resp.status_code == 405
    mock_popen.assert_not_called()


def test_config_open_data_dir_post_opens_file_manager(client, tmp_path):
    with patch("wisper_transcribe.config.get_data_dir", return_value=tmp_path), \
         patch("subprocess.Popen") as mock_popen:
        resp = client.post("/config/open-data-dir")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    mock_popen.assert_called_once()
    # The spawned command targets the data dir, nothing user-controlled.
    assert str(tmp_path) in mock_popen.call_args.args[0]


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
# Startup orphan sweep (R9-1)
# ---------------------------------------------------------------------------


def test_cleanup_orphaned_uploads_removes_all_prefixes(tmp_path, monkeypatch):
    """R9-1/R6: the startup sweep recognizes wisper_enroll_* temp files (the
    standalone speaker-enroll route's crash-window safety net) and the
    wisper_enrollsrc_* files a pending standalone enroll job was renamed to,
    not just wisper_upload_*."""
    import tempfile

    from wisper_transcribe.web.app import _cleanup_orphaned_uploads

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    upload = tmp_path / "wisper_upload_abc123.mp3"
    enroll = tmp_path / "wisper_enroll_def456.wav"
    enrollsrc = tmp_path / "wisper_enrollsrc_ghi789.mp3"
    other = tmp_path / "unrelated.txt"
    upload.write_bytes(b"x")
    enroll.write_bytes(b"x")
    enrollsrc.write_bytes(b"x")
    other.write_bytes(b"x")

    _cleanup_orphaned_uploads()

    assert not upload.exists()
    assert not enroll.exists()
    assert not enrollsrc.exists()
    assert other.exists()


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


def test_speakers_enroll_submit_enqueues_standalone_job(app, client, tmp_path, monkeypatch):
    """R6: the standalone enroll POST no longer runs WAV conversion /
    diarization / embedding extraction inside the request — it saves the
    upload (streamed, R10), hands ownership to a JOB_ENROLL job (mode
    "standalone"), and redirects to the job detail page. At submit time the
    wisper_enroll_* upload is renamed to wisper_enrollsrc_<job-id> so the
    startup sweep can never delete a pending job's file (F5 pattern)."""
    import tempfile as _tempfile

    monkeypatch.setattr(_tempfile, "tempdir", str(tmp_path))
    try:
        resp = client.post(
            "/speakers/enroll",
            data={"name": "TestSpeaker", "role": "DM", "notes": "n"},
            files={"audio": ("clip.mp3", b"fake audio bytes", "audio/mpeg")},
            follow_redirects=False,
        )
    finally:
        monkeypatch.setattr(_tempfile, "tempdir", None)

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/transcribe/jobs/")

    queue = app.state.job_queue
    job_id = location.rsplit("/", 1)[-1]
    job = queue.get(job_id)
    assert job is not None
    assert job.job_type == "enroll"
    assert job.enroll_mode == "standalone"
    assert job.enroll_params["profile_key"] == "testspeaker"
    assert job.enroll_params["display_name"] == "TestSpeaker"
    assert job.enroll_params["role"] == "DM"
    assert job.enroll_params["notes"] == "n"

    # Upload renamed out of the wisper_enroll_* sweep glob, owned by the job.
    upload = Path(job.input_path)
    assert upload.name == f"wisper_enrollsrc_{job.id}.mp3"
    assert upload.exists()
    assert not list(tmp_path.glob("wisper_enroll_*"))
    upload.unlink()  # the (unstarted) job would normally delete it


def test_speakers_enroll_submit_cleans_up_temp_file_when_submit_fails(client, tmp_path, monkeypatch):
    """R9-1/R6: if the job hand-off itself fails, the route still deletes the
    temp upload (ownership never transferred) and redirects with a generic
    error code."""
    import tempfile as _tempfile
    from wisper_transcribe.web.jobs import JobQueue

    monkeypatch.setattr(_tempfile, "tempdir", str(tmp_path))
    try:
        with patch.object(
            JobQueue, "submit_standalone_enroll", side_effect=RuntimeError("boom")
        ):
            resp = client.post(
                "/speakers/enroll",
                data={"name": "TestSpeaker", "role": "", "notes": ""},
                files={"audio": ("clip.mp3", b"fake audio bytes", "audio/mpeg")},
                follow_redirects=False,
            )
    finally:
        monkeypatch.setattr(_tempfile, "tempdir", None)

    assert resp.status_code == 303
    assert "error=enroll_failed" in resp.headers["location"]
    assert "boom" not in resp.headers["location"]
    assert not list(tmp_path.iterdir())  # no orphaned upload left behind


def test_speakers_remove_redirects(client, tmp_path):
    # R37: removal now goes through speaker_manager.remove_profile() (a
    # locked load-modify-save against the real profiles.json), mirroring
    # the rename route's real-file test pattern below rather than mocking
    # load_profiles/save_profiles in the route module (which no longer
    # calls them directly for removal).
    from wisper_transcribe.speaker_manager import load_profiles as _load

    _seed_profile_store(tmp_path)

    with patch("wisper_transcribe.speaker_manager.get_data_dir", return_value=tmp_path):
        resp = client.post("/speakers/alice/remove", follow_redirects=False)

    assert resp.status_code == 303
    assert _load(data_dir=tmp_path) == {}


def test_speakers_remove_deletes_reference_clip(client, tmp_path):
    """R9-5: the web removal route deletes the .mp3 reference clip alongside
    the .npy embedding, not just the profile entry."""
    emb_dir = _seed_profile_store(tmp_path)
    npy_path = emb_dir / "alice.npy"
    mp3_path = emb_dir / "alice.mp3"
    assert npy_path.exists() and mp3_path.exists()

    with patch("wisper_transcribe.speaker_manager.get_data_dir", return_value=tmp_path):
        resp = client.post("/speakers/alice/remove", follow_redirects=False)

    assert resp.status_code == 303
    assert not npy_path.exists()
    assert not mp3_path.exists()


def _seed_profile_store(tmp_path, key="alice", display="Alice", with_clip=True):
    """Write a real speakers.json + embedding files under tmp_path (used as
    the data dir) and return the embeddings dir."""
    import numpy as np
    from wisper_transcribe.models import SpeakerProfile
    from wisper_transcribe.speaker_manager import save_profiles as _save

    emb_dir = tmp_path / "profiles" / "embeddings"
    emb_dir.mkdir(parents=True, exist_ok=True)
    np.save(str(emb_dir / f"{key}.npy"), np.zeros(4))
    if with_clip:
        (emb_dir / f"{key}.mp3").write_bytes(b"fake mp3")

    profile = SpeakerProfile(
        name=key, display_name=display, role="DM",
        embedding_path=emb_dir / f"{key}.npy",
        enrolled_date="2026-04-07", enrollment_source="test.mp3",
    )
    _save({key: profile}, data_dir=tmp_path)
    return emb_dir


def test_speakers_rename_rekeys_profile_and_moves_files(client, tmp_path):
    """R31: the web rename route adopts the CLI's rekey semantic — the
    profile key changes and the .npy/.mp3 files move with it (previously the
    web route changed display_name only)."""
    from wisper_transcribe.speaker_manager import load_profiles as _load

    emb_dir = _seed_profile_store(tmp_path)

    with patch("wisper_transcribe.speaker_manager.get_data_dir", return_value=tmp_path), \
         patch("wisper_transcribe.campaign_manager.get_data_dir", return_value=tmp_path):
        resp = client.post(
            "/speakers/alice/rename",
            data={"new_name": "Alicia"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/speakers"
    profiles = _load(data_dir=tmp_path)
    assert "alice" not in profiles
    assert "alicia" in profiles
    assert profiles["alicia"].display_name == "Alicia"
    assert (emb_dir / "alicia.npy").exists()
    assert (emb_dir / "alicia.mp3").exists()
    assert not (emb_dir / "alice.npy").exists()
    assert not (emb_dir / "alice.mp3").exists()


def test_speakers_rename_updates_campaign_membership(client, tmp_path):
    """R31: a web rename rekeys campaign rosters too — membership (including
    the Discord ID binding) follows the profile instead of dangling."""
    from wisper_transcribe.campaign_manager import (
        add_member, bind_discord_id, create_campaign, load_campaigns,
    )

    _seed_profile_store(tmp_path)

    with patch("wisper_transcribe.speaker_manager.get_data_dir", return_value=tmp_path), \
         patch("wisper_transcribe.campaign_manager.get_data_dir", return_value=tmp_path):
        campaign = create_campaign("Test Campaign", data_dir=tmp_path)
        add_member(campaign.slug, "alice", role="player", data_dir=tmp_path)
        bind_discord_id(campaign.slug, "alice", "123456789012345678", data_dir=tmp_path)

        resp = client.post(
            "/speakers/alice/rename",
            data={"new_name": "Alicia"},
            follow_redirects=False,
        )

        campaigns = load_campaigns(data_dir=tmp_path)

    assert resp.status_code == 303
    members = campaigns[campaign.slug].members
    assert "alice" not in members
    assert "alicia" in members
    assert members["alicia"].role == "player"
    assert members["alicia"].discord_user_id == "123456789012345678"
    assert members["alicia"].profile_key == "alicia"


def test_speakers_rename_collision_redirects_generic_error(client, tmp_path):
    """R31: renaming onto an existing key fails with a generic error code —
    the CLI's collision guard, without reflecting the submitted name."""
    from wisper_transcribe.models import SpeakerProfile
    from wisper_transcribe.speaker_manager import load_profiles as _load, save_profiles as _save

    emb_dir = _seed_profile_store(tmp_path)
    profiles = _load(data_dir=tmp_path)
    profiles["bob"] = SpeakerProfile(
        name="bob", display_name="Bob", role="",
        embedding_path=emb_dir / "bob.npy",
        enrolled_date="2026-04-07", enrollment_source="t.mp3",
    )
    _save(profiles, data_dir=tmp_path)

    with patch("wisper_transcribe.speaker_manager.get_data_dir", return_value=tmp_path), \
         patch("wisper_transcribe.campaign_manager.get_data_dir", return_value=tmp_path):
        resp = client.post(
            "/speakers/alice/rename",
            data={"new_name": "Bob"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/speakers?error=rename_failed"
    profiles = _load(data_dir=tmp_path)
    assert "alice" in profiles  # unchanged


def test_speakers_rename_invalid_key_redirects_generic_error(client, tmp_path):
    """R31: a new name whose derived key fails the path-component guard is
    refused with a generic error code — never reflected into the redirect."""
    _seed_profile_store(tmp_path)

    with patch("wisper_transcribe.speaker_manager.get_data_dir", return_value=tmp_path), \
         patch("wisper_transcribe.campaign_manager.get_data_dir", return_value=tmp_path):
        resp = client.post(
            "/speakers/alice/rename",
            data={"new_name": "evil/../../name"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/speakers?error=rename_failed"
    assert "evil" not in resp.headers["location"]


def test_speakers_rename_display_case_only_keeps_key(client, tmp_path):
    """Renaming to a name that derives the same key updates display_name
    without moving files or breaking the profile key."""
    from wisper_transcribe.speaker_manager import load_profiles as _load

    emb_dir = _seed_profile_store(tmp_path, display="alice")

    with patch("wisper_transcribe.speaker_manager.get_data_dir", return_value=tmp_path), \
         patch("wisper_transcribe.campaign_manager.get_data_dir", return_value=tmp_path):
        resp = client.post(
            "/speakers/alice/rename",
            data={"new_name": "Alice"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    profiles = _load(data_dir=tmp_path)
    assert "alice" in profiles
    assert profiles["alice"].display_name == "Alice"
    assert (emb_dir / "alice.npy").exists()


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


def test_delete_transcript_also_removes_diar_sidecar_and_audio(client, tmp_path):
    """(g) F5: deleting a transcript must also remove the _diar.json sidecar
    and the durable audio copy it references -- that audio exists only to
    back the (now-deleted) transcript's enrollment wizard, so leaving it
    behind would be a permanent leak."""
    import json

    md = tmp_path / "session01.md"
    md.write_text(_TRANSCRIPT_MD)
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake-audio")
    diar = tmp_path / "session01_diar.json"
    diar.write_text(json.dumps({
        "input_path": str(audio),
        "campaign": None,
        "diarization_segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
    }), encoding="utf-8")

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post("/transcripts/session01/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert not md.exists()
    assert not audio.exists()
    assert not diar.exists()


def test_delete_transcript_also_removes_excerpt_clips(client, tmp_path):
    """R9-4: deleting a transcript removes its <stem>_excerpt_*.mp3/.txt
    speaker-preview clips, which the code previously left behind."""
    md = tmp_path / "session01.md"
    md.write_text(_TRANSCRIPT_MD)
    clip_mp3 = tmp_path / "session01_excerpt_SPEAKER_00.mp3"
    clip_txt = tmp_path / "session01_excerpt_SPEAKER_00.txt"
    clip_mp3.write_bytes(b"fake mp3")
    clip_txt.write_text("Hello there")
    # A different transcript's excerpt with a similar prefix must survive.
    unrelated = tmp_path / "session01x_excerpt_SPEAKER_00.mp3"
    unrelated.write_bytes(b"unrelated")

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post("/transcripts/session01/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert not md.exists()
    assert not clip_mp3.exists()
    assert not clip_txt.exists()
    assert unrelated.exists()
    unrelated.unlink()


def test_delete_transcript_glob_metacharacter_stem_does_not_leak_other_clips(client, tmp_path):
    """Regression: a transcript stem containing glob metacharacters (e.g.
    an uploaded file literally named 'mix*.mp3') must not turn
    _delete_excerpt_clips's pattern into a wildcard that also matches a
    DIFFERENT transcript's excerpt clips. Without glob.escape(), deleting
    "mix*" builds the pattern "mix*_excerpt_*", which also matches
    "mix2_excerpt_SPEAKER_00.mp3" -- a completely unrelated transcript."""
    from urllib.parse import quote

    victim_stem = "mix*"
    victim_md = tmp_path / f"{victim_stem}.md"
    victim_md.write_text(_TRANSCRIPT_MD)

    bystander_clip = tmp_path / "mix2_excerpt_SPEAKER_00.mp3"
    bystander_clip.write_bytes(b"unrelated transcript's clip")

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post(
            f"/transcripts/{quote(victim_stem, safe='')}/delete", follow_redirects=False
        )

    assert resp.status_code == 303
    assert not victim_md.exists()
    assert bystander_clip.exists()
    bystander_clip.unlink()


def test_delete_transcript_never_deletes_audio_outside_output_dir(client, tmp_path):
    """A legacy sidecar pointing at a path outside the output dir (e.g. a
    pre-F5 tempdir path) must never be deleted by the transcript-delete
    route -- only durable copies that actually live in the output dir."""
    import json

    md = tmp_path / "session01.md"
    md.write_text(_TRANSCRIPT_MD)
    outside_dir = tmp_path.parent / "outside_audio_dir"
    outside_dir.mkdir(exist_ok=True)
    outside_audio = outside_dir / "session01.mp3"
    outside_audio.write_bytes(b"fake-audio")
    diar = tmp_path / "session01_diar.json"
    diar.write_text(json.dumps({
        "input_path": str(outside_audio),
        "campaign": None,
        "diarization_segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
    }), encoding="utf-8")

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post("/transcripts/session01/delete", follow_redirects=False)

    assert resp.status_code == 303
    assert not md.exists()
    assert not diar.exists()
    assert outside_audio.exists()
    outside_audio.unlink()
    outside_dir.rmdir()


def test_transcribe_post_processing_flags_set_on_job(client, tmp_path):
    """Toggle checkboxes send 'on'; the job must have post_refine/post_summarize=True."""
    audio_file = tmp_path / "test.mp3"
    audio_file.write_bytes(b"fake mp3")
    with open(audio_file, "rb") as f:
        resp = client.post(
            "/transcribe",
            files={"file": ("test.mp3", f, "audio/mpeg")},
            # Browsers send "on" for checked checkboxes, not "1"
            data={"post_refine": "on", "post_summarize": "on"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/transcribe/jobs/")
    job_id = loc.split("/")[-1]
    job = client.app.state.job_queue.get(job_id)
    assert job is not None
    assert job.post_refine is True
    assert job.post_summarize is True


def test_transcribe_post_no_diarize_round_trips_to_queue(client, tmp_path, monkeypatch):
    """The Speakers `Off` cell submits no_diarize=on; the job kwarg must be True."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.web.jobs import Job
    import uuid

    fake_job = MagicMock(spec=Job)
    fake_job.id = str(uuid.uuid4())

    with patch("wisper_transcribe.web.routes.transcribe.get_output_dir", return_value=tmp_path), \
         patch.object(client.app.state.job_queue, "submit", return_value=fake_job) as mock_submit:
        resp = client.post(
            "/transcribe",
            files={"file": ("audiobook.m4b", b"fake audio", "audio/mp4")},
            data={"no_diarize": "on", "num_speakers": ""},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    call_kwargs = mock_submit.call_args.kwargs
    assert call_kwargs.get("no_diarize") is True
    assert call_kwargs.get("num_speakers") is None


def test_transcribe_post_default_no_diarize_is_false(client, tmp_path, monkeypatch):
    """Without the `Off` cell selected, no_diarize defaults to False."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.web.jobs import Job
    import uuid

    fake_job = MagicMock(spec=Job)
    fake_job.id = str(uuid.uuid4())

    with patch("wisper_transcribe.web.routes.transcribe.get_output_dir", return_value=tmp_path), \
         patch.object(client.app.state.job_queue, "submit", return_value=fake_job) as mock_submit:
        resp = client.post(
            "/transcribe",
            files={"file": ("session.mp3", b"fake audio", "audio/mpeg")},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    call_kwargs = mock_submit.call_args.kwargs
    assert call_kwargs.get("no_diarize") is False


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


def test_speakers_rename_empty_name_no_change(client, tmp_path):
    # An empty new_name short-circuits before the route ever calls
    # speaker_manager.rename_profile(), so no profiles.json write happens --
    # verified against the real store rather than mocking module-level
    # load_profiles/save_profiles (the route no longer imports the latter).
    from wisper_transcribe.speaker_manager import load_profiles as _load

    _seed_profile_store(tmp_path)

    with patch("wisper_transcribe.speaker_manager.get_data_dir", return_value=tmp_path):
        resp = client.post(
            "/speakers/alice/rename",
            data={"new_name": ""},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert _load(data_dir=tmp_path)["alice"].display_name == "Alice"  # unchanged


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


def test_campaign_remove_transcript_unlinks_stem(client, tmp_path, monkeypatch):
    """POST /campaigns/{slug}/transcripts/remove removes the stem from the campaign."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.campaign_manager import create_campaign, move_transcript_to_campaign, load_campaigns

    create_campaign("Test Game", data_dir=tmp_path)
    move_transcript_to_campaign("session-01", "test-game", data_dir=tmp_path)

    resp = client.post(
        "/campaigns/test-game/transcripts/remove",
        data={"stem": "session-01"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/campaigns/test-game" in resp.headers.get("location", "")
    assert "session-01" not in load_campaigns(tmp_path)["test-game"].transcripts


def test_campaign_remove_transcript_noop_for_absent_stem(client, tmp_path, monkeypatch):
    """Removing a stem not in the campaign is a no-op (still redirects cleanly)."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.campaign_manager import create_campaign

    create_campaign("Test Game", data_dir=tmp_path)

    resp = client.post(
        "/campaigns/test-game/transcripts/remove",
        data={"stem": "not-in-campaign"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/campaigns/test-game" in resp.headers.get("location", "")


def test_campaign_remove_transcript_rejects_traversal(client, tmp_path, monkeypatch):
    """Path-traversal and null-byte payloads in stem are rejected with 400."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.campaign_manager import create_campaign

    create_campaign("Test Game", data_dir=tmp_path)

    for bad_stem in ["../etc/passwd", "foo/bar", "stem\x00bad"]:
        resp = client.post(
            "/campaigns/test-game/transcripts/remove",
            data={"stem": bad_stem},
            follow_redirects=False,
        )
        assert resp.status_code == 400, f"expected 400 for stem={bad_stem!r}, got {resp.status_code}"


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
# Enrollment — job-centric wizard submit enqueues a JOB_ENROLL job (Phase 2.5)
#
# The synchronous enroll_speaker()/add_member() assertions that used to live
# here now live at the enroll_shared.enroll_profiles() unit-test level (see
# tests/test_transcript_enroll.py) and at the JOB_ENROLL runner level (see
# tests/test_web_jobs.py) -- the route itself no longer calls those functions
# inline, so asserting on them right after a POST would be racing the
# background job queue's worker. These tests instead verify the route's own
# responsibility: applying the rename synchronously and enqueueing (or not
# enqueueing) a job with the right payload.
# ---------------------------------------------------------------------------


def test_enroll_submit_enqueues_job_when_segments_present(
    client, tmp_path, monkeypatch
):
    """(a)+(b) job-centric POST renames synchronously and enqueues a
    JOB_ENROLL job carrying the validated rename groups."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

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

    resp = client.post(
        f"/transcribe/jobs/{job.id}/enroll",
        data={"speaker_SPEAKER_00": "Alice"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/transcribe/jobs/")
    assert location != f"/transcribe/jobs/{job.id}"  # a NEW enroll job, not the original

    enroll_job_id = location.rsplit("/", 1)[-1]
    enroll_job = client.app.state.job_queue.get(enroll_job_id)
    assert enroll_job is not None
    assert enroll_job.job_type == "enroll"
    assert enroll_job.enroll_groups == {"Alice": ["SPEAKER_00"]}

    # (b) rename applied synchronously, before the redirect
    assert "**Alice**" in transcript.read_text(encoding="utf-8")


def test_enroll_submit_skips_enroll_when_no_segments(client, tmp_path, monkeypatch):
    """No JOB_ENROLL is enqueued when the completed job has no diarization
    segments -- apply_renames() has nothing to group, so the route falls
    back to the plain transcript redirect."""
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

    resp = client.post(
        f"/transcribe/jobs/{job.id}/enroll",
        data={"speaker_SPEAKER_00": "Bob"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/transcripts/session01"
    assert all(j.job_type != "enroll" for j in client.app.state.job_queue.list_all())


def test_enroll_form_uses_diarization_segments_not_frontmatter(client, tmp_path, monkeypatch):
    """enroll_form uses raw SPEAKER_N labels from diarization_segments, even after
    the transcript has already been renamed (stale frontmatter guard)."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

    from wisper_transcribe.web.jobs import Job, COMPLETED
    from datetime import datetime
    import uuid

    # Transcript already renamed — frontmatter shows display names, not raw labels
    transcript = tmp_path / "session01.md"
    transcript.write_text(
        "---\nspeakers:\n  - name: Alice\n  - name: Bob\n---\n\n"
        "**Alice** *(00:00)*: Hello.\n**Bob** *(00:10)*: Hi."
    )

    seg_a = MagicMock()
    seg_a.speaker = "SPEAKER_00"
    seg_a.start = 0.0
    seg_b = MagicMock()
    seg_b.speaker = "SPEAKER_01"
    seg_b.start = 10.0

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(tmp_path / "audio.mp3"),
        kwargs={"device": "cpu"},
        output_path=str(transcript),
        diarization_segments=[seg_a, seg_b],
    )
    client.app.state.job_queue._jobs[job.id] = job

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        resp = client.get(f"/transcribe/jobs/{job.id}/enroll")

    assert resp.status_code == 200
    body = resp.content.decode()
    # Must show raw diarization labels, not the already-renamed display names
    assert "SPEAKER_00" in body
    assert "SPEAKER_01" in body
    assert 'name="speaker_SPEAKER_00"' in body
    assert 'name="speaker_SPEAKER_01"' in body


# ---------------------------------------------------------------------------
# Phase 1 audit fixes — F1 (legacy job-path rename no-op), F2 (junk
# "SPEAKER_XX" profiles from untouched fields), F3 (EMA merge instead of
# overwrite on resubmission / two labels -> one profile averaging)
# ---------------------------------------------------------------------------


def test_job_path_rename_works_when_transcript_has_display_names(
    client, tmp_path, monkeypatch
):
    """F1: once match_speakers has already written a display name into the
    body (e.g. from a prior session's enrollment), the job-path submit
    handler must still resolve the raw label -> current display name and
    rename correctly, instead of silently no-op'ing against a body that no
    longer contains "**SPEAKER_00**"."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

    from wisper_transcribe.web.jobs import Job, COMPLETED
    from wisper_transcribe.models import DiarizationSegment
    from datetime import datetime
    import uuid

    transcript = tmp_path / "session01.md"
    transcript.write_text(
        "---\nspeakers:\n  - name: Unknown Speaker 1\n---\n\n"
        "**Unknown Speaker 1** *(00:00)*: Hello everyone.",
        encoding="utf-8",
    )
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake")

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(audio),
        kwargs={"device": "cpu"},
        output_path=str(transcript),
        diarization_segments=[
            DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00"),
        ],
    )
    client.app.state.job_queue._jobs[job.id] = job

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker"), \
         patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p):
        resp = client.post(
            f"/transcribe/jobs/{job.id}/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    content = transcript.read_text(encoding="utf-8")
    assert "**Alice**" in content
    assert "Unknown Speaker 1" not in content


def test_job_path_get_form_prefills_current_display_name(client, tmp_path, monkeypatch):
    """F1: the GET wizard form must also resolve current_names on the job
    path (previously only the transcript-centric path did this)."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

    from wisper_transcribe.web.jobs import Job, COMPLETED
    from wisper_transcribe.models import DiarizationSegment
    from datetime import datetime
    import uuid

    transcript = tmp_path / "session01.md"
    transcript.write_text(
        "---\nspeakers:\n  - name: Brandon\n---\n\n"
        "**Brandon** *(00:00)*: Hello everyone.",
        encoding="utf-8",
    )

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(tmp_path / "audio.mp3"),
        kwargs={"device": "cpu"},
        output_path=str(transcript),
        diarization_segments=[
            DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00"),
        ],
    )
    client.app.state.job_queue._jobs[job.id] = job

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        resp = client.get(f"/transcribe/jobs/{job.id}/enroll")

    assert resp.status_code == 200
    assert 'value="Brandon"' in resp.content.decode()


def test_raw_label_shaped_submission_refused(client, tmp_path, monkeypatch):
    """F2: submitting an untouched field (value still "SPEAKER_05", pyannote's
    raw format) must not rename the transcript or enroll a junk profile."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

    from wisper_transcribe.web.jobs import Job, COMPLETED
    from wisper_transcribe.models import DiarizationSegment
    from datetime import datetime
    import uuid

    transcript = tmp_path / "session01.md"
    original = (
        "---\nspeakers:\n  - name: SPEAKER_05\n---\n\n"
        "**SPEAKER_05** *(00:00)*: Hello."
    )
    transcript.write_text(original, encoding="utf-8")
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake")

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(audio),
        kwargs={"device": "cpu"},
        output_path=str(transcript),
        diarization_segments=[
            DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_05"),
        ],
    )
    client.app.state.job_queue._jobs[job.id] = job

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker") as mock_enroll, \
         patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p):
        resp = client.post(
            f"/transcribe/jobs/{job.id}/enroll",
            data={"speaker_SPEAKER_05": "SPEAKER_05"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    mock_enroll.assert_not_called()
    assert transcript.read_text(encoding="utf-8") == original


def test_existing_profile_name_still_enqueues_job(client, tmp_path, monkeypatch):
    """F3: resubmitting a name that already has a voice profile (but the
    *transcript's* current name differs, i.e. an actual change) still groups
    and enqueues a JOB_ENROLL job -- the EMA-vs-overwrite branching itself is
    covered at the enroll_shared.enroll_profiles() unit-test level (see
    tests/test_transcript_enroll.py), since it now runs in the job worker,
    not inline with this POST."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

    from wisper_transcribe.web.jobs import Job, COMPLETED
    from wisper_transcribe.models import DiarizationSegment
    from datetime import datetime
    import uuid

    transcript = tmp_path / "session01.md"
    transcript.write_text(
        "---\nspeakers:\n  - name: SPEAKER_00\n---\n\n"
        "**SPEAKER_00** *(00:00)*: Hello.",
        encoding="utf-8",
    )
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake")

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(audio),
        kwargs={"device": "cpu"},
        output_path=str(transcript),
        diarization_segments=[
            DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00"),
        ],
    )
    client.app.state.job_queue._jobs[job.id] = job

    resp = client.post(
        f"/transcribe/jobs/{job.id}/enroll",
        data={"speaker_SPEAKER_00": "Alice"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/transcribe/jobs/")
    enroll_job = client.app.state.job_queue.get(location.rsplit("/", 1)[-1])
    assert enroll_job.job_type == "enroll"
    assert enroll_job.enroll_groups == {"Alice": ["SPEAKER_00"]}


def test_two_labels_same_name_grouped_into_one_job_entry(client, tmp_path, monkeypatch):
    """F3: when two raw pyannote labels are assigned the same display name in
    one submit (pyannote over-segmented one real speaker), apply_renames()
    must group them together on the job so enroll_profiles() (unit-tested
    separately) can average their embeddings -- not create two competing
    entries."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

    from wisper_transcribe.web.jobs import Job, COMPLETED
    from wisper_transcribe.models import DiarizationSegment
    from datetime import datetime
    import uuid

    transcript = tmp_path / "session01.md"
    transcript.write_text(
        "---\nspeakers:\n  - name: SPEAKER_00\n  - name: SPEAKER_01\n---\n\n"
        "**SPEAKER_00** *(00:00)*: Hello.\n**SPEAKER_01** *(00:10)*: Hi again.",
        encoding="utf-8",
    )
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake")

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(audio),
        kwargs={"device": "cpu"},
        output_path=str(transcript),
        diarization_segments=[
            DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00"),
            DiarizationSegment(start=10.0, end=15.0, speaker="SPEAKER_01"),
        ],
    )
    client.app.state.job_queue._jobs[job.id] = job

    resp = client.post(
        f"/transcribe/jobs/{job.id}/enroll",
        data={"speaker_SPEAKER_00": "Alice", "speaker_SPEAKER_01": "Alice"},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    location = resp.headers["location"]
    enroll_job = client.app.state.job_queue.get(location.rsplit("/", 1)[-1])
    assert enroll_job.enroll_groups == {"Alice": ["SPEAKER_00", "SPEAKER_01"]}


def test_unchanged_name_with_existing_profile_skips_enroll(client, tmp_path, monkeypatch):
    """F3: resubmitting the same (already-current) display name for a
    speaker that already has a profile must not call enroll_speaker OR
    update_embedding -- nothing changed, so nothing should be re-extracted."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))

    from wisper_transcribe.web.jobs import Job, COMPLETED
    from wisper_transcribe.models import DiarizationSegment, SpeakerProfile
    from datetime import datetime
    import uuid

    transcript = tmp_path / "session01.md"
    transcript.write_text(
        "---\nspeakers:\n  - name: Alice\n---\n\n"
        "**Alice** *(00:00)*: Hello.",
        encoding="utf-8",
    )
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"fake")

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(audio),
        kwargs={"device": "cpu"},
        output_path=str(transcript),
        diarization_segments=[
            DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00"),
        ],
    )
    client.app.state.job_queue._jobs[job.id] = job

    existing_alice = SpeakerProfile(
        name="alice", display_name="Alice", role="", embedding_path=tmp_path / "alice.npy",
        enrolled_date="2026-01-01", enrollment_source="old.mp3",
    )

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={"alice": existing_alice}), \
         patch("wisper_transcribe.speaker_manager.extract_embedding") as mock_extract, \
         patch("wisper_transcribe.speaker_manager.update_embedding") as mock_update, \
         patch("wisper_transcribe.speaker_manager.enroll_speaker") as mock_enroll, \
         patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p):
        resp = client.post(
            f"/transcribe/jobs/{job.id}/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    mock_enroll.assert_not_called()
    mock_update.assert_not_called()
    mock_extract.assert_not_called()


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


def test_bulk_delete_removes_excerpt_clips(client, tmp_path, monkeypatch):
    """R9-4: bulk-delete removes each stem's excerpt clips too."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    (tmp_path / "session01.md").write_text("# S1")
    clip = tmp_path / "session01_excerpt_SPEAKER_00.mp3"
    clip.write_bytes(b"fake mp3")

    with patch("wisper_transcribe.web.routes.transcripts.get_output_dir", return_value=tmp_path):
        resp = client.post(
            "/transcripts/bulk-delete",
            data={"stems": ["session01"]},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert not clip.exists()


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


# ---------------------------------------------------------------------------
# F9 -- GET /transcribe/jobs/{job_id}/excerpt/{speaker_name} on-disk fallback
# must be scoped to the job's own transcript stem, never glob the whole
# output directory (which could serve a different transcript's same-labelled
# excerpt clip).
# ---------------------------------------------------------------------------

def test_job_excerpt_fallback_scoped_to_job_stem(client, tmp_path):
    """When the in-memory clip_path is missing, the fallback must only look
    for `<this job's transcript stem>_excerpt_<label>.mp3` -- a decoy file
    from a different transcript with the same speaker label must never be
    served."""
    from wisper_transcribe.web.jobs import Job, COMPLETED
    from datetime import datetime
    import uuid

    transcript = tmp_path / "session01.md"
    transcript.write_text("# Session 01", encoding="utf-8")

    # The correct on-disk clip for THIS job's transcript stem.
    correct_clip = tmp_path / "session01_excerpt_SPEAKER_00.mp3"
    correct_clip.write_bytes(b"correct-clip-bytes")

    # A decoy belonging to a different transcript, same speaker label --
    # must never be served for this job.
    decoy_clip = tmp_path / "otherstem_excerpt_SPEAKER_00.mp3"
    decoy_clip.write_bytes(b"decoy-clip-bytes")

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(tmp_path / "audio.mp3"),
        kwargs={},
        output_path=str(transcript),
    )
    client.app.state.job_queue._jobs[job.id] = job

    resp = client.get(f"/transcribe/jobs/{job.id}/excerpt/SPEAKER_00")

    assert resp.status_code == 200
    assert resp.content == b"correct-clip-bytes"


def test_job_excerpt_fallback_404_when_job_gone(client, tmp_path):
    """If the in-memory job is gone (server restarted), the route can't know
    which transcript stem to scope a fallback glob to -- it must 404 rather
    than blindly serving whatever matching file happens to exist on disk."""
    decoy_clip = tmp_path / "session01_excerpt_SPEAKER_00.mp3"
    decoy_clip.write_bytes(b"should-not-be-served")

    resp = client.get("/transcribe/jobs/nonexistent-job-id/excerpt/SPEAKER_00")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# R32-8: _build_tailwind must not crash startup when input.css is missing
# ---------------------------------------------------------------------------

def test_build_tailwind_missing_input_css_does_not_raise(tmp_path, monkeypatch):
    """_INPUT_CSS.stat() used to raise an uncaught FileNotFoundError when
    input.css was absent (e.g. a stripped install), crashing app startup
    instead of falling into the existing warn-and-continue path."""
    import wisper_transcribe.web.app as app_module

    missing_input = tmp_path / "does-not-exist.css"
    output_css = tmp_path / "tailwind.min.css"
    output_css.write_text("/* stale */", encoding="utf-8")

    monkeypatch.setattr(app_module, "_INPUT_CSS", missing_input)
    monkeypatch.setattr(app_module, "_OUTPUT_CSS", output_css)

    # Should not raise, and should fall through to attempt (and gracefully
    # fail) the subprocess rebuild rather than crashing on the stat() call.
    app_module._build_tailwind()
