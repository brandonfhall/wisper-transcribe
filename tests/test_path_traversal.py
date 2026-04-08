import pytest
from fastapi.testclient import TestClient
from urllib.parse import quote
from unittest.mock import patch

from wisper_transcribe.web.app import create_app


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
    
    assert resp.status_code == 303
    # Ensure the exact, fully-encoded payload is safely contained in the relative URL
    assert resp.headers.get("location") == f"/transcribe/jobs/{safe_url}"
    assert not resp.headers.get("location", "").startswith("//")


@pytest.mark.parametrize("payload", _REDIRECT_PAYLOADS)
def test_transcribe_enroll_open_redirect_blocked(client: TestClient, payload: str):
    """Ensure job enroll route prevents open redirects/CRLF for non-completed jobs."""
    safe_url = quote(payload, safe="")
    
    from wisper_transcribe.web.jobs import Job
    from datetime import datetime
    fake_job = Job(id=payload, status="pending", created_at=datetime.now(), input_path="", kwargs={})

    with patch("wisper_transcribe.web.jobs.JobQueue.get", return_value=fake_job):
        resp = client.get(f"/transcribe/jobs/{safe_url}/enroll", follow_redirects=False)
        
    assert resp.status_code == 303
    assert resp.headers.get("location") == f"/transcribe/jobs/{safe_url}"
    assert not resp.headers.get("location", "").startswith("//")


@pytest.mark.parametrize("payload", _REDIRECT_PAYLOADS)
def test_transcribe_enroll_submit_open_redirect_blocked(client: TestClient, payload: str):
    """Ensure job enroll submit route prevents open redirects/CRLF for non-completed jobs."""
    safe_url = quote(payload, safe="")
    
    from wisper_transcribe.web.jobs import Job
    from datetime import datetime
    fake_job = Job(id=payload, status="pending", created_at=datetime.now(), input_path="", kwargs={})

    with patch("wisper_transcribe.web.jobs.JobQueue.get", return_value=fake_job):
        resp = client.post(f"/transcribe/jobs/{safe_url}/enroll", follow_redirects=False)
        
    assert resp.status_code == 303
    assert resp.headers.get("location") == f"/transcribe/jobs/{safe_url}"
    assert not resp.headers.get("location", "").startswith("//")