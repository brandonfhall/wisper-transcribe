import pytest
from fastapi.testclient import TestClient
from urllib.parse import quote
from unittest.mock import patch

from wisper_transcribe.web.app import create_app
from wisper_transcribe.web.routes.transcribe import _validate_job_id


@pytest.fixture
def client():
    """Provide a TestClient with a fresh FastAPI app."""
    app = create_app()
    return TestClient(app)


# Payloads that try to trick the file system.
# Note: "." and ".." are omitted because httpx/TestClient automatically normalizes 
# them out of the URL path before sending the request. "..." is a valid filename.
_MALICIOUS_PAYLOADS = [
    "\x00",
    "some\x00name",
]

# Payloads designed to fail the strict regex guard (^[\w\-]+$)
_REGEX_PAYLOADS = [
    "invalid*name",
    "invalid+name",
    "name!@#",
]

@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS)
def test_transcripts_path_traversal_blocked(client: TestClient, payload: str):
    """Ensure the transcript routes block directory traversal and null bytes."""
    safe_url = quote(payload)
    
    # 1. Detail view
    resp = client.get(f"/transcripts/{safe_url}")
    assert resp.status_code == 400
    assert "Invalid name" in resp.text

    # 2. Download
    resp = client.get(f"/transcripts/{safe_url}/download")
    assert resp.status_code == 400

    # 3. Delete
    resp = client.post(f"/transcripts/{safe_url}/delete")
    assert resp.status_code == 400

    # 4. Fix speaker
    resp = client.post(f"/transcripts/{safe_url}/fix-speaker", data={"old_name": "a", "new_name": "b"})
    assert resp.status_code == 400


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS)
def test_speakers_clip_path_traversal_blocked(client: TestClient, payload: str):
    """Ensure the speaker reference clip route blocks directory traversal."""
    safe_url = quote(payload)
    resp = client.get(f"/speakers/{safe_url}/clip")
    assert resp.status_code == 400
    assert "Invalid key" in resp.text


@pytest.mark.parametrize("payload", _REGEX_PAYLOADS)
def test_speakers_clip_regex_guard(client: TestClient, payload: str):
    """Ensure the speaker reference clip route enforces the strict alphanumeric regex."""
    safe_url = quote(payload)
    resp = client.get(f"/speakers/{safe_url}/clip")
    assert resp.status_code == 400
    assert "Invalid key" in resp.text


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS)
def test_speakers_enroll_path_traversal_blocked(client: TestClient, payload: str):
    """Ensure the speaker enrollment route blocks directory traversal."""
    resp = client.post("/speakers/enroll", data={"name": payload}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=invalid_name" in resp.headers.get("location", "")


@pytest.mark.parametrize("payload", _REGEX_PAYLOADS)
def test_speakers_enroll_regex_guard(client: TestClient, payload: str):
    """Ensure the speaker enrollment route enforces the strict alphanumeric regex."""
    resp = client.post("/speakers/enroll", data={"name": payload}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=invalid_name" in resp.headers.get("location", "")


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS)
def test_speakers_remove_path_traversal_blocked(client: TestClient, payload: str):
    """Ensure speaker removal handles malicious payloads gracefully (dict lookup)."""
    safe_url = quote(payload)
    resp = client.post(f"/speakers/{safe_url}/remove", follow_redirects=False)
    # It should silently fail the dict lookup and redirect back
    assert resp.status_code == 303


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS)
def test_speakers_rename_path_traversal_blocked(client: TestClient, payload: str):
    """Ensure speaker rename handles malicious payloads gracefully (dict lookup)."""
    safe_url = quote(payload)
    resp = client.post(f"/speakers/{safe_url}/rename", data={"new_name": "foo"}, follow_redirects=False)
    assert resp.status_code == 303


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS)
def test_transcribe_excerpt_path_traversal_blocked(client: TestClient, payload: str):
    """Ensure the transcribe excerpt route blocks directory traversal."""
    safe_url = quote(payload)
    # We use a fake job ID. The path traversal check should happen first and return 400
    # before it even checks if the job ID exists (which would normally return 404).
    resp = client.get(f"/transcribe/jobs/fake-job-id/excerpt/{safe_url}")
    assert resp.status_code == 400
    assert "Invalid speaker name" in resp.text


# Payloads that try to trick the redirect mechanism (Open Redirect / CRLF)
# Note: Payloads with forward slashes ("/") are omitted because FastAPI's default 
# path router strictly blocks them, returning a 404 before our handlers even run.
_REDIRECT_PAYLOADS = [
    "\\\\evil.com",
    "javascript:alert(1)",
    "\r\nLocation: evil.com",
]

@pytest.mark.parametrize("payload", _REDIRECT_PAYLOADS)
def test_transcribe_cancel_open_redirect_blocked(client: TestClient, payload: str):
    """Ensure job cancellation route prevents open redirects/CRLF."""
    safe_url = quote(payload, safe="")
    resp = client.post(f"/transcribe/jobs/{safe_url}/cancel", follow_redirects=False)
    
    assert resp.status_code == 400
    assert "Invalid job ID" in resp.text


@pytest.mark.parametrize("payload", _REDIRECT_PAYLOADS)
def test_transcribe_enroll_open_redirect_blocked(client: TestClient, payload: str):
    """Ensure job enroll route prevents open redirects/CRLF for non-completed jobs."""
    safe_url = quote(payload, safe="")
    
    from wisper_transcribe.web.jobs import Job
    from datetime import datetime
    fake_job = Job(id=payload, status="pending", created_at=datetime.now(), input_path="", kwargs={})

    with patch("wisper_transcribe.web.jobs.JobQueue.get", return_value=fake_job):
        resp = client.get(f"/transcribe/jobs/{safe_url}/enroll", follow_redirects=False)
        
    assert resp.status_code == 400
    assert "Invalid job ID" in resp.text


@pytest.mark.parametrize("payload", _REDIRECT_PAYLOADS)
def test_transcribe_enroll_submit_open_redirect_blocked(client: TestClient, payload: str):
    """Ensure job enroll submit route prevents open redirects/CRLF for non-completed jobs."""
    safe_url = quote(payload, safe="")

    from wisper_transcribe.web.jobs import Job
    from datetime import datetime
    fake_job = Job(id=payload, status="pending", created_at=datetime.now(), input_path="", kwargs={})

    with patch("wisper_transcribe.web.jobs.JobQueue.get", return_value=fake_job):
        resp = client.post(f"/transcribe/jobs/{safe_url}/enroll", follow_redirects=False)

    assert resp.status_code == 400
    assert "Invalid job ID" in resp.text


# ---------------------------------------------------------------------------
# _validate_job_id unit tests
# ---------------------------------------------------------------------------

_VALID_JOB_IDS = [
    "550e8400-e29b-41d4-a716-446655440000",  # standard UUID
    "abc-123",
    "job_id_with_underscores",
    "a1B2c3",
]

@pytest.mark.parametrize("job_id", _VALID_JOB_IDS)
def test_validate_job_id_accepts_valid_ids(job_id: str):
    """_validate_job_id must return the input unchanged for well-formed IDs."""
    assert _validate_job_id(job_id) == job_id


_INVALID_JOB_IDS = [
    "",                             # empty
    "\x00",                         # null byte
    "../../etc/passwd",             # path traversal
    "id with spaces",               # spaces not allowed
    "id/with/slashes",              # path separators
    "id\\backslash",                # backslash
    "evil\r\nLocation: evil.com",   # CRLF header injection
    "javascript:alert(1)",          # JS URI
    "\\\\evil.com",                 # UNC path attempt
    "id!@#",                        # special chars
]

@pytest.mark.parametrize("bad_id", _INVALID_JOB_IDS)
def test_validate_job_id_rejects_invalid_inputs(bad_id: str):
    """_validate_job_id must return None for any dangerous or malformed input."""
    assert _validate_job_id(bad_id) is None


# ---------------------------------------------------------------------------
# Speaker enroll — error redirect must not leak internal exception text
# ---------------------------------------------------------------------------

def test_speakers_enroll_error_does_not_leak_exception(client: TestClient):
    """A failed enrollment must redirect with a generic code, not exception details."""
    with patch("wisper_transcribe.config.load_config", return_value={}), \
         patch("wisper_transcribe.config.get_device", return_value="cpu"), \
         patch("wisper_transcribe.config.get_hf_token", return_value="tok"), \
         patch(
             "wisper_transcribe.audio_utils.convert_to_wav",
             side_effect=RuntimeError("secret internal path: /home/user/.config"),
         ):
        resp = client.post(
            "/speakers/enroll",
            files={"audio": ("clip.mp3", b"fake", "audio/mpeg")},
            data={"name": "Alice"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    location = resp.headers.get("location", "")
    assert "enroll_failed" in location
    # The exception message must NOT appear anywhere in the redirect URL
    assert "secret" not in location
    assert "internal" not in location
    assert "home" not in location

# ---------------------------------------------------------------------------
# Campaign slug path traversal + open-redirect guards
# ---------------------------------------------------------------------------

_CAMPAIGN_SLUG_PAYLOADS = [
    "\x00",
    "../etc/passwd",
    "a/b/c",
    "evil\r\nHeader: injected",
    "javascript:alert(1)",
    ".",
    "..",
]


@pytest.mark.parametrize("payload", _CAMPAIGN_SLUG_PAYLOADS)
def test_campaigns_detail_path_traversal_blocked(client, payload):
    from urllib.parse import quote
    resp = client.get(f"/campaigns/{quote(payload, safe='')}", follow_redirects=False)
    # 400/303: our slug validator rejected the payload.
    # 404: routing layer rejected it (multi-segment paths like a/b/c or ../etc/passwd
    #      don't match the single-segment {slug} parameter after URL normalisation).
    # 200: Starlette normalised . or .. to the parent path, serving the safe campaigns
    #      index — no campaign detail operation was executed with the traversal slug.
    assert resp.status_code in (200, 303, 400, 404)
    location = resp.headers.get("location", "")
    # Location header must never carry raw traversal characters.
    assert "\x00" not in location
    assert ".." not in location


@pytest.mark.parametrize("payload", _CAMPAIGN_SLUG_PAYLOADS)
def test_campaigns_delete_path_traversal_blocked(client, payload):
    from urllib.parse import quote
    resp = client.post(
        f"/campaigns/{quote(payload, safe='')}/delete", follow_redirects=False
    )
    # 303/400: our validator rejected; 404/405: routing rejected after normalisation.
    assert resp.status_code in (303, 400, 404, 405)


@pytest.mark.parametrize("payload", _CAMPAIGN_SLUG_PAYLOADS)
def test_campaigns_add_member_path_traversal_blocked(client, payload):
    from urllib.parse import quote
    resp = client.post(
        f"/campaigns/{quote(payload, safe='')}/members",
        data={"profile_key": "alice", "role": "", "character": ""},
        follow_redirects=False,
    )
    assert resp.status_code in (303, 400, 404, 405)


@pytest.mark.parametrize("payload", _CAMPAIGN_SLUG_PAYLOADS)
def test_campaigns_remove_member_path_traversal_blocked(client, payload):
    from urllib.parse import quote
    resp = client.post(
        f"/campaigns/{quote(payload, safe='')}/members/alice/remove",
        follow_redirects=False,
    )
    assert resp.status_code in (303, 400, 404, 405)


def test_campaigns_create_error_does_not_leak_exception(client, tmp_path, monkeypatch):
    """A create_campaign failure must produce generic ?error= code, not exception text."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    with patch(
        "wisper_transcribe.web.routes.campaigns.create_campaign",
        side_effect=ValueError("internal path /home/secret revealed"),
    ):
        resp = client.post(
            "/campaigns",
            data={"display_name": "Test"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    location = resp.headers.get("location", "")
    assert "error=create_failed" in location
    assert "secret" not in location
    assert "internal" not in location
    assert "home" not in location


# ---------------------------------------------------------------------------
# Recording ID path traversal + validation
# ---------------------------------------------------------------------------

# Note: "." and ".." are omitted because httpx/TestClient automatically normalizes
# them out of the URL path before sending the request. The _validate_recording_id
# unit test below still covers these cases.
_RECORDING_ID_PAYLOADS = [
    "\x00",
    "../etc/passwd",
    "a/b/c",
    "evil\r\nHeader: injected",
    "invalid*name",
    "id\\backslash",
]

@pytest.mark.parametrize("payload", _RECORDING_ID_PAYLOADS)
def test_recordings_api_path_traversal_blocked(client, payload):
    from urllib.parse import quote
    safe = quote(payload, safe="")

    # GET /api/recordings/{id}
    resp = client.get(f"/api/recordings/{safe}")
    assert resp.status_code in (400, 404)

    # POST /api/recordings/{id}/transcribe
    resp = client.post(f"/api/recordings/{safe}/transcribe")
    assert resp.status_code in (400, 404)

    # POST /api/recordings/{id}/delete
    resp = client.post(f"/api/recordings/{safe}/delete")
    assert resp.status_code in (400, 404)


@pytest.mark.parametrize("payload", _RECORDING_ID_PAYLOADS)
def test_recordings_html_path_traversal_blocked(client, payload):
    from urllib.parse import quote
    safe = quote(payload, safe="")

    # GET /recordings/{id} (HTML detail)
    resp = client.get(f"/recordings/{safe}", follow_redirects=False)
    assert resp.status_code in (200, 303, 400, 404)

    # POST /recordings/{id}/delete (HTML form)
    resp = client.post(f"/recordings/{safe}/delete", follow_redirects=False)
    assert resp.status_code in (303, 400, 404, 405)

    # POST /recordings/{id}/enroll (HTML form)
    resp = client.post(
        f"/recordings/{safe}/enroll",
        data={"discord_user_id": "123456789012345678", "profile_name": "Test"},
        follow_redirects=False,
    )
    assert resp.status_code in (303, 400, 404, 409)

    # POST /recordings/{id}/transcribe (HTML form)
    resp = client.post(f"/recordings/{safe}/transcribe", follow_redirects=False)
    assert resp.status_code in (303, 400, 404)

    # GET /recordings/{id}/live
    resp = client.get(f"/recordings/{safe}/live")
    assert resp.status_code in (400, 404, 501)


def test_validate_recording_id_accepts_valid_ids():
    from wisper_transcribe.recording_manager import _validate_recording_id
    for rid in [
        "550e8400-e29b-41d4-a716-446655440000",
        "abc-123",
        "valid_id_123",
        "a1B2c3",
    ]:
        assert _validate_recording_id(rid) is not None, f"should accept {rid!r}"


def test_validate_recording_id_rejects_invalid_ids():
    from wisper_transcribe.recording_manager import _validate_recording_id
    for rid in [
        "",
        "\x00",
        "../etc/passwd",
        "a/b/c",
        "evil\r\n",
        "id!@#",
        "id\\backslash",
        ".",
        "..",
        "invalid*name",
    ]:
        assert _validate_recording_id(rid) is None, f"should reject {rid!r}"


def test_validate_campaign_slug_accepts_valid_slugs():
    from wisper_transcribe.campaign_manager import _validate_campaign_slug
    for slug in ["dnd-mondays", "abc_123", "UPPER", "my-campaign-1"]:
        assert _validate_campaign_slug(slug) is not None, f"should accept {slug!r}"


def test_validate_campaign_slug_rejects_invalid_slugs():
    from wisper_transcribe.campaign_manager import _validate_campaign_slug
    for slug in ["", "\x00", "../etc", "a/b", "evil\r\n", "javascript:x", ".", ".."]:
        assert _validate_campaign_slug(slug) is None, f"should reject {slug!r}"
