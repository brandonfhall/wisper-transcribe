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

@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS + _REGEX_PAYLOADS + [
    "../etc/passwd", "id/with/slashes", "evil\r\nLocation: x",
])
def test_transcript_routes_take_only_integer_ids(client: TestClient, payload: str):
    """A non-integer segment never matches a transcript id route.

    The GET falls through to the legacy name handler, which redirects without
    touching a path; every POST is a 404/405. Nothing is deleted or submitted.
    """
    safe_url = quote(payload, safe="")

    resp = client.get(f"/transcripts/{safe_url}", follow_redirects=False)
    # 303: the legacy handler found no row; 404: the path never matched a route.
    assert resp.status_code in (400, 404, 303)
    location = resp.headers.get("location", "")
    assert payload not in location
    assert "\x00" not in location and ".." not in location

    resp = client.post(f"/transcripts/{safe_url}/delete", follow_redirects=False)
    assert resp.status_code in (404, 405)


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS + _REGEX_PAYLOADS + [
    "../escape", "..\\escape", "a/b", "evil\r\nLocation: x",
])
def test_transcript_excerpt_speaker_name_guard(client: TestClient, payload: str, tmp_path, monkeypatch):
    """A speaker label on the transcript excerpt route never reaches the
    filesystem: the integer id resolves the transcript, and the label is
    sanitised by the shared lookup inside that transcript's folder."""
    from wisper_transcribe import transcript_store

    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out))
    md = out / "s1.md"
    md.write_text("x", encoding="utf-8")
    tid = transcript_store.register(md, origin="reconcile")

    safe_url = quote(payload, safe="")
    resp = client.get(f"/transcripts/{tid}/excerpt/{safe_url}", follow_redirects=False)
    assert resp.status_code in (400, 404)
    assert payload not in resp.text


# ---------------------------------------------------------------------------
# Legacy /transcripts/{name} redirect
# ---------------------------------------------------------------------------

def test_legacy_name_url_redirects_to_the_id_when_unique(client, tmp_path, monkeypatch):
    """An old name link reaches the legacy handler (not a 422/404) and 303s to
    the id-based URL."""
    from wisper_transcribe import transcript_store

    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out))
    md = out / "Session 1.md"
    md.write_text("x", encoding="utf-8")
    tid = transcript_store.register(md, origin="reconcile")

    resp = client.get("/transcripts/" + quote("Session 1"), follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/transcripts/{tid}"


def test_legacy_name_url_chooses_between_same_named_campaigns(client, tmp_path, monkeypatch):
    """A name held by two campaigns offers a chooser listing both by id."""
    from wisper_transcribe import db, file_registry
    from . import _seed

    data = tmp_path / "data"
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("WISPER_DATA_DIR", str(data))
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out))
    cid_a = _seed.seed_campaign("Alpha", slug="alpha", claimed=True)
    cid_b = _seed.seed_campaign("Beta", slug="beta", claimed=True)

    ids = {}
    for cid, folder in ((cid_a, "Alpha"), (cid_b, "Beta")):
        md = out / folder / "Session 1.md"
        md.write_text("x", encoding="utf-8")
        with db.transaction() as conn:
            tid = conn.execute(
                "INSERT INTO transcripts (stem, campaign_id, position, created_at) "
                "VALUES (?, ?, 0, ?) RETURNING id",
                ("Session 1", cid, db.now_utc()),
            ).fetchone()[0]
        file_registry.add(md, kind="transcript", owner=file_registry.Owner("transcript", tid),
                          output_dir=out)
        ids[cid] = tid

    resp = client.get("/transcripts/" + quote("Session 1"), follow_redirects=False)
    assert resp.status_code == 200
    assert f"/transcripts/{ids[cid_a]}" in resp.text
    assert f"/transcripts/{ids[cid_b]}" in resp.text
    assert "Alpha" in resp.text and "Beta" in resp.text


def test_legacy_name_url_unknown_redirects_to_not_found(client):
    resp = client.get("/transcripts/no-such-session", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/transcripts?error=not_found"


def test_not_found_integer_id_redirects_to_not_found(client):
    resp = client.get("/transcripts/999999", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/transcripts?error=not_found"



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


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS + _REGEX_PAYLOADS)
def test_transcribe_excerpt_job_present_path_traversal_blocked(
    client: TestClient, payload: str, tmp_path
):
    """The on-disk excerpt fallback (job present, in-memory clip_path missing/stale)
    builds a path from the sanitised speaker label -- confirm a malicious or
    regex-busting speaker_name never results in a served file, even with a
    real job (and a same-stem excerpt clip) present, not just the
    job-not-found short-circuit the other test above exercises."""
    from wisper_transcribe.web.jobs import Job, COMPLETED
    from datetime import datetime
    import uuid

    transcript = tmp_path / "session01.md"
    transcript.write_text("# Session 01", encoding="utf-8")
    # A legitimately-named clip that must never be served for a malicious
    # or malformed speaker_name.
    (tmp_path / "session01_excerpt_SPEAKER_00.mp3").write_bytes(b"clip-bytes")

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(tmp_path / "audio.mp3"),
        kwargs={},
        output_path=str(transcript),
    )
    client.app.state.job_queue._jobs[job.id] = job

    safe_url = quote(payload)
    resp = client.get(f"/transcribe/jobs/{job.id}/excerpt/{safe_url}")
    assert resp.status_code != 200


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
    """A failed enrollment hand-off must redirect with a generic code, not
    exception details. The route's only failure surface is job submission;
    the job's own generic errors are tested in test_web_jobs.py."""
    from wisper_transcribe.web.jobs import JobQueue

    with patch.object(
        JobQueue,
        "submit_standalone_enroll",
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
def test_campaigns_journal_download_path_traversal_blocked(client, payload):
    from urllib.parse import quote
    resp = client.get(
        f"/campaigns/{quote(payload, safe='')}/journal/download", follow_redirects=False
    )
    # 400: our validator rejected; 404: routing or unknown campaign.
    assert resp.status_code in (400, 404)
    disposition = resp.headers.get("content-disposition", "")
    assert "\x00" not in disposition and ".." not in disposition
    assert "\r" not in disposition and "\n" not in disposition


@pytest.mark.parametrize("payload", _CAMPAIGN_SLUG_PAYLOADS)
def test_campaigns_relabel_path_traversal_blocked(client, payload):
    from urllib.parse import quote
    with patch.object(client.app.state.job_queue, "submit_relabel") as mock_submit:
        resp = client.post(
            f"/campaigns/{quote(payload, safe='')}/relabel", follow_redirects=False
        )
    # 303/400: our validator rejected; 404/405: routing rejected after normalisation.
    assert resp.status_code in (303, 400, 404, 405)
    mock_submit.assert_not_called()
    location = resp.headers.get("location", "")
    assert "\x00" not in location
    assert ".." not in location
    assert "\r" not in location and "\n" not in location


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


@pytest.mark.parametrize("payload", [
    "../etc/passwd",
    "..\\escape",
    "a/b/c",
    "evil\r\nLocation: x",
    "name\x00inject",
])
def test_campaigns_create_display_name_never_reaches_a_path(client, payload):
    """A campaign name is sanitized into one folder name; no payload becomes a path."""
    from wisper_transcribe.config import get_output_root

    resp = client.post("/campaigns", data={"display_name": payload}, follow_redirects=False)
    assert resp.status_code in (303, 400)
    location = resp.headers.get("location", "")
    assert payload not in location
    assert "\x00" not in location
    assert "\r" not in location and "\n" not in location

    root = get_output_root()
    # Nothing was created outside the output root, and no entry escapes it.
    for entry in root.iterdir():
        assert entry.resolve().parent == root.resolve()


@pytest.mark.parametrize("payload", [
    "../etc/passwd",
    "a/b",
    "a\\b",
    "evil\r\nLocation: x",
    "name\x00inject",
])
def test_campaigns_rename_route_never_escapes_the_output_root(client, payload):
    """The rename route's slug and display_name never build a path or leak into a redirect."""
    from wisper_transcribe.config import get_output_root
    from wisper_transcribe.campaign_manager import create_campaign

    create_campaign("Game")
    resp = client.post(f"/campaigns/{quote(payload, safe='')}/rename",
                       data={"display_name": payload}, follow_redirects=False)
    assert resp.status_code in (303, 400, 404, 405)
    location = resp.headers.get("location", "")
    assert "\x00" not in location
    assert "\r" not in location and "\n" not in location

    root = get_output_root()
    for entry in root.iterdir():
        assert entry.resolve().parent == root.resolve()


@pytest.mark.parametrize("payload", [
    "../etc/passwd",
    "a/b",
    "a\\b",
    "evil\r\nLocation: x",
    "name\x00inject",
])
def test_campaigns_finish_rename_route_rejects_a_bad_slug(client, payload):
    from wisper_transcribe.config import get_output_root

    resp = client.post(f"/campaigns/{quote(payload, safe='')}/finish-rename",
                       follow_redirects=False)
    assert resp.status_code in (303, 400, 404, 405)
    location = resp.headers.get("location", "")
    assert "\x00" not in location
    assert "\r" not in location and "\n" not in location
    root = get_output_root()
    for entry in root.iterdir():
        assert entry.resolve().parent == root.resolve()


def test_campaigns_rename_error_does_not_leak_exception(client, tmp_path, monkeypatch):
    """A rename failure must produce a generic ?error= code, not exception text."""
    from wisper_transcribe.campaign_folders import RenameOutcome

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.campaign_manager import create_campaign
    create_campaign("Game")
    with patch(
        "wisper_transcribe.web.routes.campaigns.rename_campaign",
        return_value=RenameOutcome("internal /home/secret"),
    ):
        resp = client.post("/campaigns/game/rename", data={"display_name": "X"},
                           follow_redirects=False)

    location = resp.headers.get("location", "")
    assert "error=rename_failed" in location
    assert "secret" not in location and "home" not in location


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




# ---------------------------------------------------------------------------
# Shared excerpt-clip lookup helper (enroll_shared.find_excerpt_clip)
# ---------------------------------------------------------------------------

def test_find_excerpt_clip_traversal_payloads_stay_inside_out_dir(tmp_path):
    """Traversal-shaped labels must never resolve outside the output dir —
    the helper's re.sub whitelist + abspath/startswith guard neutralise them."""
    from wisper_transcribe.web.enroll_shared import find_excerpt_clip

    out_dir = tmp_path / "output"
    out_dir.mkdir()
    secret = tmp_path / "secret.mp3"
    secret.write_bytes(b"outside")

    for payload in ["../secret", "..\\secret", "a/../../secret", "\x00", "x\x00y"]:
        assert find_excerpt_clip(out_dir, "stem", [payload]) is None


def test_find_excerpt_clip_regex_busting_labels_are_sanitised(tmp_path):
    """Labels with regex-busting characters are collapsed to underscores and
    only match a file that literally carries the sanitised name."""
    from wisper_transcribe.web.enroll_shared import find_excerpt_clip

    out_dir = tmp_path / "output"
    out_dir.mkdir()
    clip = out_dir / "stem_excerpt_invalid_name.mp3"
    clip.write_bytes(b"audio")

    found = find_excerpt_clip(out_dir, "stem", ["invalid*name"])
    assert found == clip


def test_find_excerpt_clip_returns_first_existing_candidate(tmp_path):
    from wisper_transcribe.web.enroll_shared import find_excerpt_clip

    out_dir = tmp_path / "output"
    out_dir.mkdir()
    legacy = out_dir / "stem_excerpt_Alice.mp3"
    legacy.write_bytes(b"audio")

    found = find_excerpt_clip(out_dir, "stem", ["SPEAKER_00", "Alice"])
    assert found == legacy


def test_find_excerpt_clip_missing_returns_none(tmp_path):
    from wisper_transcribe.web.enroll_shared import find_excerpt_clip

    out_dir = tmp_path / "output"
    out_dir.mkdir()
    assert find_excerpt_clip(out_dir, "stem", ["SPEAKER_00"]) is None


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS + _REGEX_PAYLOADS + [
    "invalid*name", "invalid+name", "name!@#",
])
def test_transcript_bulk_campaign_rejects_a_malformed_campaign_slug(client, payload):
    """The bulk campaign route is id-based; only the campaign slug is a
    user string, and it must pass the slug guard."""
    resp = client.post("/transcripts/bulk-campaign",
                       data={"transcript_id": ["1"], "campaign": payload},
                       follow_redirects=False)
    assert resp.status_code in (303, 400, 422)
    assert resp.headers.get("location", "") in ("", "/transcripts?error=invalid_campaign")


# ---------------------------------------------------------------------------
# Rename target name (form field) flows into file paths via the profile
# key — must be guarded like any other path component
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS + _REGEX_PAYLOADS + [
    "../escape", "a/b", "..", "with space/../x",
])
def test_speakers_rename_new_name_path_guard(client: TestClient, payload: str, tmp_path, monkeypatch):
    """The web rename rekeys the profile (moves its .mp3 clip), so the
    submitted new name must pass the path-component guard; hostile names are
    refused with a generic error code and never reflected."""
    from wisper_transcribe.speaker_manager import load_profiles, reference_clip_path

    from ._seed import seed_profile

    seed_profile("alice", "Alice", data_dir=tmp_path)
    clip = reference_clip_path("alice", tmp_path)
    clip.parent.mkdir(parents=True, exist_ok=True)
    clip.write_bytes(b"mp3")

    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    resp = client.post(
        "/speakers/alice/rename",
        data={"new_name": payload},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/speakers?error=rename_failed"
    # Nothing renamed, no file escaped or moved
    assert set(load_profiles(tmp_path)) == {"alice"}
    assert clip.exists()


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS + _REGEX_PAYLOADS + [
    "../escape.mp3", "../../etc/passwd", "a/b.mp3", "evil\r\nLocation: x.mp3",
])
def test_transcribe_name_check_never_escapes_output_dir(client, payload, tmp_path, monkeypatch):
    """The name check resolves only inside the output dir and echoes nothing."""
    out = tmp_path / "out"
    out.mkdir()
    (tmp_path / "escape.md").write_text("outside", encoding="utf-8")
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out))
    resp = client.get("/transcribe/name-check", params={"filename": payload})
    assert resp.status_code == 200
    assert resp.json() == {"exists": False, "campaign": None, "modified": None,
                           "missing": False, "clashes": [], "overwrite_allowed": True}
    assert payload not in resp.text


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS + _REGEX_PAYLOADS + [
    "../escape", "../../etc/passwd", "a/b", "..", "evil\r\nLocation: x",
])
def test_transcribe_name_check_campaign_param_is_inert(client, payload, tmp_path, monkeypatch):
    """The campaign slug is only a lookup key; a malformed one resolves to
    the root and is never reflected or used as a path."""
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out))
    resp = client.get("/transcribe/name-check",
                      params={"filename": "session.mp3", "campaign": payload})
    assert resp.status_code == 200
    assert resp.json() == {"exists": False, "campaign": None, "modified": None,
                           "missing": False, "clashes": [], "overwrite_allowed": True}
    assert payload not in resp.text


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS + _REGEX_PAYLOADS + [
    "../escape", "../../etc/passwd", "a/b", "..", "evil\r\nLocation: x",
])
def test_campaigns_relink_accepts_only_integer_ids(client, payload, tmp_path, monkeypatch):
    """The campaign relink route takes database ids; a malformed one is
    refused rather than looked up as a name, and never reflected."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.campaign_manager import create_campaign

    create_campaign("Game")
    for form in ({"old_id": payload, "new_id": "1"}, {"old_id": "1", "new_id": payload}):
        resp = client.post("/campaigns/game/transcripts/relink", data=form, follow_redirects=False)
        assert resp.status_code in (303, 400, 422)
        location = resp.headers.get("location", "")
        assert location in ("", "/campaigns/game?error=relink_failed")
        assert payload not in location


_UNSAFE_NAMES = _MALICIOUS_PAYLOADS + _REGEX_PAYLOADS + [
    "../escape", "../../etc/passwd", "a/b", "..", "evil\r\nLocation: x",
]


@pytest.mark.parametrize("payload", _UNSAFE_NAMES)
def test_transcripts_relink_rejects_non_integer_ids(client, payload, tmp_path, monkeypatch):
    """Both ids come from form data; a malformed one is refused and never
    reflected into the redirect."""
    for form in ({"old_id": payload, "new_id": "1"}, {"old_id": "1", "new_id": payload}):
        resp = client.post("/transcripts/relink", data=form, follow_redirects=False)
        assert resp.status_code in (303, 400, 422)
        assert resp.headers.get("location", "") in ("", "/transcripts?error=relink_failed")
        assert payload not in resp.text


@pytest.mark.parametrize("payload", _UNSAFE_NAMES + ["../outside.summary.md",
                                                     "..\\outside.summary.md",
                                                     "sub/x.summary.md", "x.summary.md\x00.txt"])
def test_needs_attention_delete_file_never_leaves_the_output_dir(client, payload, tmp_path):
    """The file name is a basename with a companion-file pattern, inside the
    output root; anything else deletes nothing."""
    from wisper_transcribe.path_utils import get_output_dir

    out = get_output_dir()
    outside = out.parent / "outside.summary.md"
    outside.write_text("keep", encoding="utf-8")
    resp = client.post("/transcripts/needs-attention/delete-file", data={"name": payload},
                       follow_redirects=False)
    assert resp.status_code in (303, 400, 422)
    assert resp.headers.get("location", "") in ("", "/transcripts?error=delete_failed")
    assert payload not in resp.text
    assert outside.exists()


@pytest.mark.parametrize("payload", ["Game/../../outside.summary.md", "../Game/x.summary.md",
                                     "Game/sub/x.summary.md", "Game\\x.summary.md",
                                     "Game/x.summary.md\x00", "Not A Campaign/x.summary.md",
                                     "Game/..", "/Game/x.summary.md"])
def test_needs_attention_delete_file_folder_form_stays_in_a_campaign_folder(client, payload):
    """``<folder>/<file>`` names only a current campaign's folder, one level deep."""
    from wisper_transcribe.path_utils import get_output_dir

    from . import _seed

    _seed.seed_campaign("Game", claimed=True)
    out = get_output_dir()
    outside = out.parent / "outside.summary.md"
    outside.write_text("keep", encoding="utf-8")
    resp = client.post("/transcripts/needs-attention/delete-file", data={"name": payload},
                       follow_redirects=False)
    assert resp.status_code in (303, 400, 422)
    assert resp.headers.get("location", "") in ("", "/transcripts?error=delete_failed")
    assert payload not in resp.text
    assert outside.exists()


@pytest.mark.parametrize("route", ["claim-folder", "recreate-folder"])
@pytest.mark.parametrize("payload", ["x", "1.5", "../1", "1; DROP TABLE campaigns", "\x00", "",
                                     "99999"])
def test_needs_attention_folder_actions_take_only_a_campaign_id(client, route, payload):
    resp = client.post(f"/transcripts/needs-attention/{route}", data={"campaign_id": payload},
                       follow_redirects=False)
    assert resp.status_code in (303, 400, 422)
    assert resp.headers.get("location", "") in ("", "/transcripts?error=not_found")
    assert payload not in resp.text or payload == ""


@pytest.mark.parametrize("payload", ["x", "1.5", "-1", "1; DROP TABLE files", "\x00", ""])
def test_needs_attention_forget_takes_only_a_listed_file_id(client, payload):
    resp = client.post("/transcripts/needs-attention/forget", data={"file_id": payload},
                       follow_redirects=False)
    assert resp.status_code in (303, 400, 422)
    assert resp.headers.get("location", "") in ("", "/transcripts?error=forget_failed")


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS + _REGEX_PAYLOADS + ["\x00", "a\x00b", "\r\nSet-Cookie: x=1"])
def test_search_params_are_inert(client: TestClient, payload: str):
    """/search reads no files and never redirects: every parameter is either a
    search term or ignored unless it exactly matches a dropdown value."""
    r = client.get("/search", params={"q": payload, "campaign": payload, "speaker": payload,
                                      "kind": payload})
    assert r.status_code == 200
    assert "Set-Cookie" not in r.headers
    assert "Traceback" not in r.text


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS + ["\x00", '"</script>'])
def test_transcript_highlight_param_is_inert(client: TestClient, payload: str, tmp_path, monkeypatch):
    """The ?q= highlight parameter on transcript and summary pages is never a path."""
    from wisper_transcribe import transcript_store

    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out))
    md = out / "nope.md"
    md.write_text("x", encoding="utf-8")
    tid = transcript_store.register(md, origin="reconcile")
    for url in (f"/transcripts/{tid}", f"/transcripts/{tid}/summary"):
        r = client.get(url, params={"q": payload})
        # The summary has no sidecar yet: 404. The detail page renders: 200.
        assert r.status_code in (200, 404)


@pytest.mark.parametrize("key", ["../outside", "..\\outside", "sub/../../outside", "outside\x00"])
def test_remove_profile_files_stays_in_clips_dir(tmp_path, key):
    """A key from a URL never deletes a file outside the reference-clips folder."""
    from wisper_transcribe.speaker_manager import get_reference_clips_dir, remove_profile_files

    clips = get_reference_clips_dir(tmp_path)
    clips.mkdir(parents=True, exist_ok=True)
    outside = clips.parent / "outside.mp3"
    outside.write_bytes(b"keep")
    try:
        remove_profile_files(key, tmp_path)
    except ValueError:
        pass  # a null byte is rejected outright
    assert outside.read_bytes() == b"keep"


def test_remove_profile_files_deletes_its_own_clip(tmp_path):
    from wisper_transcribe.speaker_manager import get_reference_clips_dir, remove_profile_files

    clips = get_reference_clips_dir(tmp_path)
    clips.mkdir(parents=True, exist_ok=True)
    (clips / "joe_(dm).mp3").write_bytes(b"x")
    remove_profile_files("joe_(dm)", tmp_path)
    assert not (clips / "joe_(dm).mp3").exists()


@pytest.mark.parametrize("payload", ["..%2Foutside", "a%2Fb", "..%5Coutside"])
def test_transcript_audio_rejects_slashes(client: TestClient, payload: str):
    """An encoded slash or backslash never reaches the filesystem."""
    assert client.get(f"/transcripts/{payload}/audio").status_code in (400, 404)


@pytest.mark.parametrize("payload", [quote(p, safe="") for p in _MALICIOUS_PAYLOADS + _REGEX_PAYLOADS] + [
    "..%2Foutside", "a%2Fb", "..%5Coutside", "evil%0D%0ALocation:%20x", "%5C%5Cevil.com"])
def test_transcript_retranscribe_never_submits_for_a_malformed_id(client, payload):
    """A non-integer transcript id never matches the route, so no job is queued."""
    queue = client.app.state.job_queue
    with patch.object(queue, "submit") as submit:
        resp = client.post(f"/transcripts/{payload}/retranscribe", follow_redirects=False)
    assert resp.status_code in (404, 405)
    assert "evil" not in resp.headers.get("location", "")
    submit.assert_not_called()


def test_transcript_retranscribe_error_redirect_uses_the_id(client, tmp_path, monkeypatch):
    from wisper_transcribe import transcript_store

    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out))
    md = out / "Session — 1!.md"
    md.write_text("x", encoding="utf-8")
    tid = transcript_store.register(md, origin="job")
    resp = client.post(f"/transcripts/{tid}/retranscribe", follow_redirects=False)
    assert resp.headers["location"] == f"/transcripts/{tid}?error=no_audio"


# ---------------------------------------------------------------------------
# Move / rename: the new_name form field and the GET clash page's query values
# ---------------------------------------------------------------------------

def _moved_setup(tmp_path, monkeypatch):
    """A registered session in the root; returns (id, md)."""
    from wisper_transcribe import transcript_store

    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("WISPER_OUTPUT_DIR", str(out))
    md = out / "Session 1.md"
    md.write_text("x", encoding="utf-8")
    return transcript_store.register(md, origin="job"), md


@pytest.mark.parametrize("payload", ["\x00", "some\x00name", "invalid*name", "../escape",
                                    "../../etc/passwd", "a/b", "..", "evil\r\nLocation: x",
                                    ".hidden", ".wisper-tmp-1", "COM1", "x" * 101,
                                    "Notes.md", "x.summary"])
def test_rename_route_rejects_every_unsafe_new_name(client, payload, tmp_path, monkeypatch):
    """A hostile or invalid new_name is refused with a fixed code, no file
    touched, and never reflected into the response."""
    tid, md = _moved_setup(tmp_path, monkeypatch)

    resp = client.post(f"/transcripts/{tid}/rename", data={"new_name": payload},
                       follow_redirects=False)
    assert resp.status_code in (303, 400, 422)
    assert resp.headers.get("location", "") == f"/transcripts/{tid}?error=invalid_name"
    assert payload not in resp.text
    assert md.exists() and md.read_text(encoding="utf-8") == "x"


def test_rename_route_renames_only_within_its_folder(client, tmp_path, monkeypatch):
    """A valid rename keeps the file in its folder and never escapes it."""
    tid, md = _moved_setup(tmp_path, monkeypatch)
    resp = client.post(f"/transcripts/{tid}/rename", data={"new_name": "Renamed"},
                       follow_redirects=False)
    assert resp.headers["location"] == f"/transcripts/{tid}"
    assert (md.parent / "Renamed.md").exists() and not md.exists()


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS + _REGEX_PAYLOADS + [
    "../escape", "../../etc/passwd", "a/b", "..", "evil\r\nLocation: x",
    ".hidden", ".wisper-tmp-1", "COM1", "x" * 101,
])
def test_rename_clash_page_query_name_is_validated_before_disk_access(
        client, payload, tmp_path, monkeypatch):
    """?clash=rename&name=… is validated by validate_new_stem; an unsafe name
    renders no clash panel and touches no disk."""
    tid, md = _moved_setup(tmp_path, monkeypatch)
    resp = client.get(f"/transcripts/{tid}", params={"clash": "rename", "name": payload})
    assert resp.status_code == 200
    assert 'data-testid="clash-panel"' not in resp.text
    assert payload not in resp.text or payload == ""
    assert md.exists()


@pytest.mark.parametrize("payload", _MALICIOUS_PAYLOADS + _REGEX_PAYLOADS + [
    "../escape", "../../etc/passwd", "a/b", "..", "evil\r\nLocation: x",
])
def test_move_clash_page_query_slug_is_validated(client, payload, tmp_path, monkeypatch):
    """?clash=move&to=… is validated with the campaign-slug guard; a hostile
    slug renders no panel and never leaks into the page."""
    tid, md = _moved_setup(tmp_path, monkeypatch)
    resp = client.get(f"/transcripts/{tid}", params={"clash": "move", "to": payload})
    assert resp.status_code == 200
    assert payload not in resp.text or payload == ""
    assert md.exists()


@pytest.mark.parametrize("payload", ["x", "1.5", "-1", "1; DROP TABLE transcripts", ""])
def test_move_files_route_takes_only_an_integer_id(client, payload, tmp_path, monkeypatch):
    """The move-files route is id-based; a malformed id never matches, so no move happens."""
    resp = client.post(f"/transcripts/{payload}/move-files", follow_redirects=False)
    assert resp.status_code in (400, 404, 405)
    assert "\x00" not in resp.headers.get("location", "")


@pytest.mark.parametrize("payload", ["\x00", "some\x00name", "invalid*name", "invalid+name",
                                    "../escape", "../../etc/passwd", "a/b", "..",
                                    "evil\r\nLocation: x", "javascript:alert(1)"])
def test_assign_campaign_form_slug_is_guarded(client, payload, tmp_path, monkeypatch):
    """The campaign form field is a lookup key and never a path; a malformed
    slug is refused with a fixed code and never reflected."""
    tid, md = _moved_setup(tmp_path, monkeypatch)

    resp = client.post(f"/transcripts/{tid}/campaign", data={"campaign": payload},
                       follow_redirects=False)
    assert resp.status_code in (303, 400, 422)
    assert resp.headers.get("location", "") == f"/transcripts/{tid}?error=invalid_campaign"
    assert payload not in resp.text
    assert md.exists()
