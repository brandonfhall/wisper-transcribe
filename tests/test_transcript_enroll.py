"""Tests for the transcript-centric enrollment wizard and _diar.json sidecar."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    from wisper_transcribe.web.app import create_app
    return create_app()


@pytest.fixture()
def client(app):
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


_SAMPLE_MD = """\
---
title: Session 01
date_processed: '2026-05-14'
speakers:
- name: SPEAKER_00
- name: SPEAKER_01
---

# Session 01

**SPEAKER_00** *(00:00)*: Hello everyone
**SPEAKER_01** *(00:12)*: Thanks for having me
"""

_SAMPLE_DIAR = {
    "input_path": "/tmp/session01.mp3",
    "campaign": "d-d-mondays",
    "diarization_segments": [
        {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
        {"start": 12.0, "end": 18.0, "speaker": "SPEAKER_01"},
    ],
}


def _write_transcript(tmp_path: Path, diar: dict | None = _SAMPLE_DIAR) -> Path:
    md = tmp_path / "session01.md"
    md.write_text(_SAMPLE_MD, encoding="utf-8")
    if diar is not None:
        (tmp_path / "session01_diar.json").write_text(
            json.dumps(diar), encoding="utf-8"
        )
    return md


def _patch_output(tmp_path: Path):
    return patch(
        "wisper_transcribe.web.routes.transcripts.get_output_dir",
        return_value=tmp_path,
    )


# ---------------------------------------------------------------------------
# Sidecar writer (_write_enrollment_sidecar in jobs.py)
# ---------------------------------------------------------------------------

def test_sidecar_written_after_job_completes(tmp_path: Path):
    """_write_enrollment_sidecar writes a _diar.json alongside the transcript."""
    from wisper_transcribe.web.jobs import _write_enrollment_sidecar, Job, COMPLETED
    from wisper_transcribe.models import DiarizationSegment
    from datetime import datetime
    import uuid

    out_md = tmp_path / "session01.md"
    out_md.write_text("# Session 01", encoding="utf-8")

    # Use the platform-native string form of the path so the assertion
    # matches the sidecar value on both POSIX and Windows.
    input_path_str = str(Path("/tmp/session01.mp3"))

    seg = DiarizationSegment(start=1.0, end=5.0, speaker="SPEAKER_00")
    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=input_path_str,
        kwargs={"campaign": "my-campaign"},
        output_path=str(out_md),
        diarization_segments=[seg],
    )

    _write_enrollment_sidecar(job, out_md)

    sidecar = tmp_path / "session01_diar.json"
    assert sidecar.exists()
    data = json.loads(sidecar.read_text())
    assert data["input_path"] == input_path_str
    assert data["campaign"] == "my-campaign"
    assert len(data["diarization_segments"]) == 1
    assert data["diarization_segments"][0] == {"start": 1.0, "end": 5.0, "speaker": "SPEAKER_00"}


def test_sidecar_includes_speaker_map_when_job_provides_it(tmp_path: Path):
    """(d) F7: when the job carries the authoritative speaker_map (populated
    from pipeline.process_file()'s _result_store), it is persisted into the
    sidecar alongside the diarization segments."""
    from wisper_transcribe.web.jobs import _write_enrollment_sidecar, Job, COMPLETED
    from wisper_transcribe.models import DiarizationSegment
    from datetime import datetime
    import uuid

    out_md = tmp_path / "session01.md"
    out_md.write_text("# Session 01", encoding="utf-8")

    seg = DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00")
    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(tmp_path / "session01.mp3"),
        kwargs={},
        output_path=str(out_md),
        diarization_segments=[seg],
        speaker_map={"SPEAKER_00": "Alice"},
    )

    _write_enrollment_sidecar(job, out_md)

    sidecar = json.loads((tmp_path / "session01_diar.json").read_text())
    assert sidecar["speaker_map"] == {"SPEAKER_00": "Alice"}


def test_sidecar_not_written_when_no_segments(tmp_path: Path):
    """_write_enrollment_sidecar is a no-op when diarization_segments is empty."""
    from wisper_transcribe.web.jobs import _write_enrollment_sidecar, Job, COMPLETED
    from datetime import datetime
    import uuid

    out_md = tmp_path / "session01.md"
    out_md.write_text("# Session 01", encoding="utf-8")
    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path="/tmp/session01.mp3",
        kwargs={},
        output_path=str(out_md),
        diarization_segments=[],
    )

    _write_enrollment_sidecar(job, out_md)

    assert not (tmp_path / "session01_diar.json").exists()


# ---------------------------------------------------------------------------
# GET /transcripts/{name}/enroll
# ---------------------------------------------------------------------------

def test_enroll_form_renders_with_sidecar(client: TestClient, tmp_path: Path):
    _write_transcript(tmp_path)
    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        resp = client.get("/transcripts/session01/enroll")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "SPEAKER_00" in body
    assert "SPEAKER_01" in body
    assert 'name="speaker_SPEAKER_00"' in body


def test_enroll_form_404_when_no_sidecar(client: TestClient, tmp_path: Path):
    _write_transcript(tmp_path, diar=None)
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/enroll")
    assert resp.status_code == 404


def test_enroll_form_404_when_no_transcript(client: TestClient, tmp_path: Path):
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/nonexistent/enroll")
    assert resp.status_code == 404


def test_enroll_form_prefills_previously_applied_names(client: TestClient, tmp_path: Path):
    """After a first rename pass, the wizard must show the applied display
    names in the input fields so the user can come back and fix a typo
    without losing what they typed."""
    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\nspeakers:\n- name: Brandon\n- name: Sam\n---\n\n"
        "**Brandon** *(00:00)*: Hello everyone\n"
        "**Sam** *(00:12)*: Thanks for having me\n",
        encoding="utf-8",
    )
    (tmp_path / "session01_diar.json").write_text(
        json.dumps(_SAMPLE_DIAR), encoding="utf-8",
    )
    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        resp = client.get("/transcripts/session01/enroll")
    body = resp.content.decode()
    assert 'name="speaker_SPEAKER_00"' in body
    assert 'value="Brandon"' in body
    assert 'name="speaker_SPEAKER_01"' in body
    assert 'value="Sam"' in body


def test_enroll_form_orders_speakers_by_first_appearance(client: TestClient, tmp_path: Path):
    """Speaker list must be ordered by first segment start time, not dict order."""
    diar = {
        "input_path": "/tmp/a.mp3",
        "campaign": None,
        "diarization_segments": [
            {"start": 10.0, "end": 15.0, "speaker": "SPEAKER_01"},
            {"start": 0.5, "end": 4.0, "speaker": "SPEAKER_00"},
        ],
    }
    _write_transcript(tmp_path, diar=diar)
    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        resp = client.get("/transcripts/session01/enroll")
    body = resp.content.decode()
    assert body.index("SPEAKER_00") < body.index("SPEAKER_01")


# ---------------------------------------------------------------------------
# POST /transcripts/{name}/enroll
# ---------------------------------------------------------------------------

def test_enroll_submit_renames_transcript(client: TestClient, tmp_path: Path):
    # The sidecar points to /tmp/session01.mp3 which won't exist in CI —
    # enrollment is silently skipped, but the rename always happens first.
    md = _write_transcript(tmp_path)
    with _patch_output(tmp_path):
        resp = client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "Alice", "speaker_SPEAKER_01": "Bob"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    content = md.read_text(encoding="utf-8")
    assert "**Alice**" in content
    assert "**Bob**" in content
    assert "**SPEAKER_00**" not in content


def test_enroll_submit_second_pass_corrects_typo(client: TestClient, tmp_path: Path):
    """A second pass through the wizard must actually update the transcript.
    The form key is still the raw label, but the markdown body now contains
    the previously-applied display name — the submit handler has to translate
    raw_label -> current_display before calling update_speaker_names."""
    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\nspeakers:\n- name: Bradnon\n- name: Sam\n---\n\n"
        "**Bradnon** *(00:00)*: Hello everyone\n"
        "**Sam** *(00:12)*: Thanks for having me\n",
        encoding="utf-8",
    )
    (tmp_path / "session01_diar.json").write_text(
        json.dumps(_SAMPLE_DIAR), encoding="utf-8",
    )
    with _patch_output(tmp_path):
        resp = client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "Brandon", "speaker_SPEAKER_01": "Sam"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    content = md.read_text(encoding="utf-8")
    assert "**Brandon**" in content
    assert "**Bradnon**" not in content
    assert "**Sam**" in content


# ---------------------------------------------------------------------------
# F6/F7 -- resolve_current_names() and apply_renames()'s single-pass,
# block-level rename (see enroll_shared.py for the full design rationale).
# ---------------------------------------------------------------------------

def _diar_segments(*pairs: tuple[str, float, float]):
    from wisper_transcribe.models import DiarizationSegment
    return [DiarizationSegment(start=start, end=end, speaker=sp) for sp, start, end in pairs]


def test_resolve_current_names_prefers_sidecar_speaker_map(tmp_path: Path):
    """(f) When the sidecar carries an authoritative speaker_map, it is used
    as-is -- no interval reconstruction happens at all."""
    from wisper_transcribe.web.enroll_shared import resolve_current_names

    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\n---\n\n**Wrong Name** *(00:00)*: hi\n",
        encoding="utf-8",
    )
    diar = {
        "speaker_map": {"SPEAKER_00": "Alice"},
        "diarization_segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}],
    }
    # If the interval heuristic ran, it would read "Wrong Name" out of the
    # markdown -- proving the persisted map (not the markdown) won.
    result = resolve_current_names(md, diar, diar["diarization_segments"])
    assert result == {"SPEAKER_00": "Alice"}


def test_resolve_current_names_falls_back_to_interval_heuristic_for_legacy_sidecar(
    tmp_path: Path,
):
    """(f) A sidecar written before the speaker_map key existed (or with no
    sidecar at all) falls back to build_legacy_label_map's interval match."""
    from wisper_transcribe.web.enroll_shared import resolve_current_names

    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\n---\n\n**Alice** *(00:00)*: hi\n",
        encoding="utf-8",
    )
    segments = _diar_segments(("SPEAKER_00", 0.0, 5.0))
    legacy_diar = {"diarization_segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}]}

    # No speaker_map key at all (legacy sidecar).
    assert resolve_current_names(md, legacy_diar, segments) == {"SPEAKER_00": "Alice"}
    # No sidecar dict whatsoever.
    assert resolve_current_names(md, None, segments) == {"SPEAKER_00": "Alice"}
    # speaker_map present but empty also falls back (defensive).
    assert resolve_current_names(md, {"speaker_map": {}, **legacy_diar}, segments) == {
        "SPEAKER_00": "Alice"
    }


def test_apply_renames_swap_on_reentry(tmp_path: Path):
    """(a) Renaming Alice->Bob and Bob->Alice in the SAME submit must swap
    both speakers' blocks correctly, not merge everyone into one name (the
    old sequential update_speaker_names loop would merge here)."""
    from wisper_transcribe.web.enroll_shared import apply_renames

    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\nspeakers:\n- name: Alice\n- name: Bob\n---\n\n"
        "**Alice** *(00:00)*: Hello everyone\n"
        "**Bob** *(00:12)*: Thanks for having me\n",
        encoding="utf-8",
    )
    segments = _diar_segments(("SPEAKER_00", 0.0, 5.0), ("SPEAKER_01", 12.0, 18.0))

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        result = apply_renames(
            md, segments, {"SPEAKER_00": "Bob", "SPEAKER_01": "Alice"},
        )

    content = md.read_text(encoding="utf-8")
    assert "**Bob** *(00:00)*: Hello everyone" in content
    assert "**Alice** *(00:12)*: Thanks for having me" in content
    # Frontmatter swaps too (both old names were unambiguous, one raw label each).
    assert "- name: Bob" in content
    assert "- name: Alice" in content
    assert result.groups == {"Bob": ["SPEAKER_00"], "Alice": ["SPEAKER_01"]}


def test_apply_renames_shared_display_name_only_renames_targeted_label(tmp_path: Path):
    """(b) Two raw labels currently both display "Dan" (many-to-one naming,
    F3). Renaming only SPEAKER_01 to "Sara" must leave SPEAKER_00's block
    saying "Dan" -- a name-keyed global rename would have renamed both."""
    from wisper_transcribe.web.enroll_shared import apply_renames

    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\nspeakers:\n- name: Dan\n---\n\n"
        "**Dan** *(00:00)*: line from the first Dan\n"
        "**Dan** *(00:12)*: line from the second Dan\n",
        encoding="utf-8",
    )
    segments = _diar_segments(("SPEAKER_00", 0.0, 5.0), ("SPEAKER_01", 12.0, 18.0))

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        result = apply_renames(md, segments, {"SPEAKER_01": "Sara"})

    content = md.read_text(encoding="utf-8")
    assert "**Dan** *(00:00)*: line from the first Dan" in content
    assert "**Sara** *(00:12)*: line from the second Dan" in content
    assert "**Dan** *(00:12)*" not in content
    # Frontmatter left alone -- "Dan" is ambiguous (shared by two raw labels),
    # so there is no safe way to know which entry to rewrite.
    assert "- name: Dan" in content
    assert result.groups == {"Sara": ["SPEAKER_01"]}


def test_apply_renames_new_name_collides_with_untouched_speaker(tmp_path: Path):
    """(c) Renaming SPEAKER_00 (Alice) to "Carol" -- the same name an
    untouched SPEAKER_01 already has -- must not fold SPEAKER_01 into the
    rename/enrollment: only SPEAKER_00 was submitted."""
    from wisper_transcribe.web.enroll_shared import apply_renames

    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\nspeakers:\n- name: Alice\n- name: Carol\n---\n\n"
        "**Alice** *(00:00)*: hi\n"
        "**Carol** *(00:12)*: hello\n",
        encoding="utf-8",
    )
    segments = _diar_segments(("SPEAKER_00", 0.0, 5.0), ("SPEAKER_01", 12.0, 18.0))

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        result = apply_renames(md, segments, {"SPEAKER_00": "Carol"})

    content = md.read_text(encoding="utf-8")
    assert "**Carol** *(00:00)*: hi" in content
    assert "**Carol** *(00:12)*: hello" in content
    # Only the explicitly-submitted raw label is grouped for enrollment --
    # SPEAKER_01 never entered `renames` and must not be swept in just
    # because the display names now collide.
    assert result.groups == {"Carol": ["SPEAKER_00"]}


def test_apply_renames_body_rename_without_timestamps(tmp_path: Path):
    """`include_timestamps=False` transcripts render "**Speaker**: text"
    with no inline timestamp at all -- there is no timing signal to
    attribute a block from. As long as the sidecar carries the authoritative
    speaker_map (guaranteed for any transcript produced after this fix), the
    per-block rename still works via the unambiguous-name fallback."""
    from wisper_transcribe.web.enroll_shared import apply_renames

    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\nspeakers:\n- name: Alice\n- name: Bob\n---\n\n"
        "**Alice**: Hello everyone\n"
        "**Bob**: Thanks for having me\n",
        encoding="utf-8",
    )
    (tmp_path / "session01_diar.json").write_text(
        json.dumps({
            "input_path": str(tmp_path / "session01.mp3"),
            "campaign": None,
            "speaker_map": {"SPEAKER_00": "Alice", "SPEAKER_01": "Bob"},
            "diarization_segments": [
                {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
                {"start": 12.0, "end": 18.0, "speaker": "SPEAKER_01"},
            ],
        }),
        encoding="utf-8",
    )
    segments = _diar_segments(("SPEAKER_00", 0.0, 5.0), ("SPEAKER_01", 12.0, 18.0))

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        result = apply_renames(md, segments, {"SPEAKER_00": "Alicia"})

    content = md.read_text(encoding="utf-8")
    assert "**Alicia**: Hello everyone" in content
    assert "**Bob**: Thanks for having me" in content
    assert "**Alice**:" not in content
    assert result.groups == {"Alicia": ["SPEAKER_00"]}


def test_apply_renames_legacy_sidecar_low_confidence_block_uses_name_fallback(
    tmp_path: Path,
):
    """F7's known-fragile regime: a legacy sidecar (no persisted speaker_map)
    where one block's rendered timestamp falls *outside every interval* and
    is numerically closer to the WRONG speaker's interval -- the nearest-
    start fallback alone would misattribute it. Because that block's text
    unambiguously says "Alice" and only one raw label currently displays
    that name, the per-block name-based fallback overrides the bad guess
    and the rename still lands on the correct (Alice/SPEAKER_00) block."""
    from wisper_transcribe.web.enroll_shared import apply_renames

    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\nspeakers:\n- name: Alice\n- name: Bob\n---\n\n"
        # Anchor blocks: both timestamps land squarely inside their own
        # interval, so build_legacy_label_map confidently resolves
        # SPEAKER_00 -> Alice and SPEAKER_01 -> Bob from these two lines.
        "**Alice** *(00:05)*: an early Alice line\n"
        "**Bob** *(16:40)*: a Bob line\n"
        # Whisper-segment start (00:17) falls between the two pyannote
        # turns ([0,10] and [20,30]) but is numerically closer to
        # SPEAKER_01's start (20) than SPEAKER_00's (0) -- nearest-start
        # alone would (wrongly) attribute this Alice line to SPEAKER_01.
        "**Alice** *(00:17)*: a later Alice line, timestamp between turns\n",
        encoding="utf-8",
    )
    # No speaker_map key at all -- legacy sidecar, forces the interval
    # heuristic (build_legacy_label_map) rather than the persisted map.
    (tmp_path / "session01_diar.json").write_text(
        json.dumps({
            "input_path": str(tmp_path / "session01.mp3"),
            "campaign": None,
            "diarization_segments": [
                {"start": 0.0, "end": 10.0, "speaker": "SPEAKER_00"},
                {"start": 20.0, "end": 30.0, "speaker": "SPEAKER_01"},
                {"start": 1000.0, "end": 1010.0, "speaker": "SPEAKER_01"},
            ],
        }),
        encoding="utf-8",
    )
    segments = _diar_segments(
        ("SPEAKER_00", 0.0, 10.0),
        ("SPEAKER_01", 20.0, 30.0),
        ("SPEAKER_01", 1000.0, 1010.0),
    )

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        result = apply_renames(md, segments, {"SPEAKER_00": "Alicia"})

    content = md.read_text(encoding="utf-8")
    assert "**Alicia** *(00:05)*: an early Alice line" in content
    assert "**Alicia** *(00:17)*: a later Alice line, timestamp between turns" in content
    assert "**Bob** *(16:40)*: a Bob line" in content
    assert result.groups == {"Alicia": ["SPEAKER_00"]}


def test_apply_renames_updates_sidecar_speaker_map(tmp_path: Path):
    """(e) After a successful rename, the sidecar's speaker_map is updated
    so the next wizard visit (or apply_renames call) resolves from it
    directly instead of re-deriving from rendered markdown."""
    from wisper_transcribe.web.enroll_shared import apply_renames

    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\nspeakers:\n- name: SPEAKER_00\n---\n\n"
        "**SPEAKER_00** *(00:00)*: hi\n",
        encoding="utf-8",
    )
    sidecar_path = tmp_path / "session01_diar.json"
    sidecar_path.write_text(
        json.dumps({
            "input_path": str(tmp_path / "session01.mp3"),
            "campaign": None,
            "diarization_segments": [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"}],
        }),
        encoding="utf-8",
    )
    segments = _diar_segments(("SPEAKER_00", 0.0, 5.0))

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        apply_renames(md, segments, {"SPEAKER_00": "Alice"})

    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert sidecar["speaker_map"] == {"SPEAKER_00": "Alice"}

    # A second call (re-entry) must now resolve "Alice" as the old name via
    # the persisted map, not by re-deriving from markdown timestamps.
    from wisper_transcribe.web.enroll_shared import _load_diar_sidecar, resolve_current_names
    diar = _load_diar_sidecar(md)
    assert resolve_current_names(md, diar, segments) == {"SPEAKER_00": "Alice"}


def test_apply_renames_frontmatter_speakers_list_updated(tmp_path: Path):
    """(g) No regression: a plain rename still updates the frontmatter
    `speakers:` list (now via formatter.rewrite_frontmatter_speakers, F11)."""
    from wisper_transcribe.web.enroll_shared import apply_renames

    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\nspeakers:\n- name: SPEAKER_00\n---\n\n"
        "**SPEAKER_00** *(00:00)*: hi\n",
        encoding="utf-8",
    )
    segments = _diar_segments(("SPEAKER_00", 0.0, 5.0))

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        apply_renames(md, segments, {"SPEAKER_00": "Alice"})

    content = md.read_text(encoding="utf-8")
    assert "- name: Alice" in content
    assert "- name: SPEAKER_00" not in content


def test_enroll_submit_enqueues_job_and_renames_synchronously(
    client: TestClient, tmp_path: Path
):
    """(a)+(b): with an existing audio file, POST applies the rename to the
    transcript synchronously, then enqueues a JOB_ENROLL job for the slow
    WAV-convert + embedding-extraction step (Phase 2.5) and redirects there
    instead of running it inline."""
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake-mp3")
    diar = {**_SAMPLE_DIAR, "input_path": str(audio)}
    md = _write_transcript(tmp_path, diar=diar)

    with _patch_output(tmp_path):
        resp = client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/transcribe/jobs/")

    # (b) rename already applied to disk before the redirect
    content = md.read_text(encoding="utf-8")
    assert "**Alice**" in content
    assert "**SPEAKER_00**" not in content

    # (a) job enqueued with the validated rename groups
    job_id = location.rsplit("/", 1)[-1]
    job = client.app.state.job_queue.get(job_id)
    assert job is not None
    assert job.job_type == "enroll"
    assert job.enroll_groups == {"Alice": ["SPEAKER_00"]}


def test_enroll_submit_skips_enroll_when_audio_missing(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """(c) No JOB_ENROLL is enqueued when the source audio is already known
    missing -- the F5 pre-check happens synchronously in the route."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    diar = {**_SAMPLE_DIAR, "input_path": "/nonexistent/audio.mp3"}
    _write_transcript(tmp_path, diar=diar)

    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker") as mock_enroll:
        client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )

    mock_enroll.assert_not_called()
    assert client.app.state.job_queue.list_all() == []


def test_enroll_submit_redirects_with_notice_when_audio_missing(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """(e) When enrollment is skipped due to missing audio, the redirect must
    carry a generic notice flag (no paths/exception text) so the detail page
    can tell the user."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    diar = {**_SAMPLE_DIAR, "input_path": "/nonexistent/audio.mp3"}
    _write_transcript(tmp_path, diar=diar)

    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker"):
        resp = client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location == "/transcripts/session01?notice=enroll_audio_missing"


def test_enroll_submit_no_notice_when_audio_present(
    client: TestClient, tmp_path: Path
):
    """No missing-audio notice when the audio is present -- the wizard
    instead enqueues an enrollment job and redirects there (c, inverse)."""
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake-mp3")
    diar = {**_SAMPLE_DIAR, "input_path": str(audio)}
    _write_transcript(tmp_path, diar=diar)

    with _patch_output(tmp_path):
        resp = client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/transcribe/jobs/")
    assert "notice=enroll_audio_missing" not in location


def test_transcript_detail_shows_notice_banner(client: TestClient, tmp_path: Path):
    """(e) The transcript detail page renders the skipped-enrollment notice
    when the redirect included the generic ?notice=enroll_audio_missing flag."""
    _write_transcript(tmp_path)
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01?notice=enroll_audio_missing")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "voice enrollment was skipped" in body.lower()


def test_transcript_detail_no_banner_without_notice(client: TestClient, tmp_path: Path):
    _write_transcript(tmp_path)
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "voice enrollment was skipped" not in body.lower()


def test_enroll_form_shows_audio_missing_banner(client: TestClient, tmp_path: Path):
    """(f) GET wizard renders a warning banner when the sidecar's input_path
    doesn't exist, so users know before they submit."""
    diar = {**_SAMPLE_DIAR, "input_path": "/nonexistent/audio.mp3"}
    _write_transcript(tmp_path, diar=diar)
    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        resp = client.get("/transcripts/session01/enroll")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "voice enrollment unavailable" in body.lower()


def test_enroll_form_no_banner_when_audio_present(client: TestClient, tmp_path: Path):
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake-mp3")
    diar = {**_SAMPLE_DIAR, "input_path": str(audio)}
    _write_transcript(tmp_path, diar=diar)
    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        resp = client.get("/transcripts/session01/enroll")
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "voice enrollment unavailable" not in body.lower()


def test_enroll_submit_404_when_no_sidecar(client: TestClient, tmp_path: Path):
    _write_transcript(tmp_path, diar=None)
    with _patch_output(tmp_path):
        resp = client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "Alice"},
            follow_redirects=False,
        )
    assert resp.status_code == 404


def test_enroll_submit_no_renames_redirects_without_write(
    client: TestClient, tmp_path: Path
):
    md = _write_transcript(tmp_path)
    original = md.read_text(encoding="utf-8")
    with _patch_output(tmp_path):
        resp = client.post(
            "/transcripts/session01/enroll",
            data={},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert md.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# GET /transcripts/{name}/excerpt/{speaker_name}
# ---------------------------------------------------------------------------

def test_excerpt_serves_clip(client: TestClient, tmp_path: Path):
    _write_transcript(tmp_path)
    clip = tmp_path / "session01_excerpt_SPEAKER_00.mp3"
    clip.write_bytes(b"fake-mp3-data")
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/excerpt/SPEAKER_00")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"


def test_excerpt_falls_back_to_legacy_display_name(client: TestClient, tmp_path: Path):
    """Pre-fix transcripts saved excerpt files keyed by display name (e.g.
    'Unknown_Speaker_1') instead of the raw pyannote label. The wizard +
    excerpt route must locate them via timestamp-matched markdown parsing
    so users don't have to re-transcribe."""
    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\n---\n\n"
        "**Unknown Speaker 1** *(00:00)*: Hello everyone\n"
        "**Unknown Speaker 2** *(00:12)*: Thanks for having me\n",
        encoding="utf-8",
    )
    (tmp_path / "session01_diar.json").write_text(
        json.dumps(_SAMPLE_DIAR), encoding="utf-8",
    )
    # Legacy file name: keyed by display name, not raw label
    legacy_clip = tmp_path / "session01_excerpt_Unknown_Speaker_1.mp3"
    legacy_clip.write_bytes(b"fake-mp3-data")

    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/excerpt/SPEAKER_00")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"


def test_enroll_form_finds_legacy_display_name_excerpts(client: TestClient, tmp_path: Path):
    """The wizard page must surface excerpts even when files are keyed by
    the old display-name convention."""
    md = tmp_path / "session01.md"
    md.write_text(
        "---\ntitle: Session 01\n---\n\n"
        "**Unknown Speaker 1** *(00:00)*: Hello everyone\n"
        "**Unknown Speaker 2** *(00:12)*: Thanks for having me\n",
        encoding="utf-8",
    )
    (tmp_path / "session01_diar.json").write_text(
        json.dumps(_SAMPLE_DIAR), encoding="utf-8",
    )
    (tmp_path / "session01_excerpt_Unknown_Speaker_1.mp3").write_bytes(b"audio")
    (tmp_path / "session01_excerpt_Unknown_Speaker_1.txt").write_text(
        "Hello everyone", encoding="utf-8",
    )

    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/enroll")
    assert resp.status_code == 200
    # Sample button is rendered when speaker_excerpts contains the raw label
    assert b"Sample" in resp.content
    # Italic snippet is rendered when speaker_excerpt_texts contains the raw label
    assert b"Hello everyone" in resp.content


def test_legacy_backfill_uses_interval_match_not_exact_timestamp(client: TestClient, tmp_path: Path):
    """The legacy backfill must match by *interval containment*, not exact
    start-time string match. In real transcripts the markdown's first-block
    timestamp is the first whisper segment's start, which usually differs
    from pyannote's first-segment-of-SPEAKER_XX start (whisper skips silence,
    aligner may assign nearby segments to UNKNOWN, etc.)."""
    md = tmp_path / "session01.md"
    # Pyannote says SPEAKER_00 spans 0.0-30.0, but the first whisper line
    # assigned to SPEAKER_00 starts at 02:15 (135s) — well inside the turn
    # but NOT equal to the pyannote start of 0.0.
    md.write_text(
        "---\ntitle: Session 01\n---\n\n"
        "**Unknown Speaker 1** *(02:15)*: Mid-turn whisper segment\n",
        encoding="utf-8",
    )
    diar = {
        "input_path": "/tmp/session01.mp3",
        "campaign": None,
        "diarization_segments": [
            # Single long pyannote turn covering the markdown timestamp
            {"start": 0.0, "end": 300.0, "speaker": "SPEAKER_00"},
        ],
    }
    (tmp_path / "session01_diar.json").write_text(json.dumps(diar), encoding="utf-8")
    (tmp_path / "session01_excerpt_Unknown_Speaker_1.mp3").write_bytes(b"audio")

    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/excerpt/SPEAKER_00")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"


def test_extract_speaker_excerpts_uses_raw_labels(tmp_path: Path):
    """`_extract_speaker_excerpts` must key files by raw pyannote label
    (`SPEAKER_00`), not the rendered display name from the markdown."""
    from wisper_transcribe.web.jobs import _extract_speaker_excerpts, Job, COMPLETED
    from wisper_transcribe.models import AlignedSegment
    from datetime import datetime
    import uuid

    out_md = tmp_path / "session01.md"
    out_md.write_text(
        "**Unknown Speaker 1** *(00:00)*: Hello\n", encoding="utf-8",
    )
    fake_audio = tmp_path / "audio.mp3"
    fake_audio.write_bytes(b"fake")

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(fake_audio),
        kwargs={},
        output_path=str(out_md),
    )
    aligned = [
        AlignedSegment(start=0.0, end=5.0, text="Hello", speaker="SPEAKER_00"),
        AlignedSegment(start=10.0, end=15.0, text="Hi", speaker="SPEAKER_00"),
        AlignedSegment(start=20.0, end=25.0, text="Goodbye", speaker="UNKNOWN"),
    ]

    with patch("wisper_transcribe.web.jobs.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        _extract_speaker_excerpts(job, out_md, aligned_segments=aligned)

    # The text file is written even if ffmpeg is mocked
    txt_path = tmp_path / "session01_excerpt_SPEAKER_00.txt"
    assert txt_path.exists()
    assert txt_path.read_text(encoding="utf-8") == "Hello"

    # UNKNOWN segments are skipped
    assert not (tmp_path / "session01_excerpt_UNKNOWN.txt").exists()

    # ffmpeg was invoked with the raw-label output path
    cmd = mock_run.call_args[0][0]
    assert any("SPEAKER_00" in str(p) for p in cmd)


def test_extract_speaker_excerpts_cuts_at_longest_segment(tmp_path: Path):
    """F10a: `_extract_speaker_excerpts` must cut the clip at the LONGEST
    aligned segment for a label, not the first occurrence -- a short
    misattributed interjection ("Yeah") would otherwise dominate the clip
    and play mostly someone else's voice. The persisted `.txt` snippet must
    come from that same longest segment."""
    from wisper_transcribe.web.jobs import _extract_speaker_excerpts, Job, COMPLETED
    from wisper_transcribe.models import AlignedSegment
    from datetime import datetime
    import uuid

    out_md = tmp_path / "session01.md"
    out_md.write_text("# Session 01\n", encoding="utf-8")
    fake_audio = tmp_path / "audio.mp3"
    fake_audio.write_bytes(b"fake")

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(fake_audio),
        kwargs={},
        output_path=str(out_md),
    )
    aligned = [
        # Short first occurrence -- likely a misattributed interjection.
        AlignedSegment(start=1.0, end=1.5, text="Yeah", speaker="SPEAKER_00"),
        # This is the LONGEST SPEAKER_00 segment -- the clip should start here.
        AlignedSegment(start=50.0, end=80.0, text="This is the real content.", speaker="SPEAKER_00"),
        # A mid-length segment that is neither first nor longest.
        AlignedSegment(start=20.0, end=25.0, text="Something in between.", speaker="SPEAKER_00"),
    ]

    with patch("wisper_transcribe.web.jobs.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        _extract_speaker_excerpts(job, out_md, aligned_segments=aligned)

    cmd = mock_run.call_args[0][0]
    ss_index = cmd.index("-ss")
    assert cmd[ss_index + 1] == "50.0"

    txt_path = tmp_path / "session01_excerpt_SPEAKER_00.txt"
    assert txt_path.read_text(encoding="utf-8") == "This is the real content."


def test_excerpt_404_when_no_clip(client: TestClient, tmp_path: Path):
    _write_transcript(tmp_path)
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/excerpt/SPEAKER_99")
    assert resp.status_code == 404


def test_excerpt_rejects_null_byte(client: TestClient, tmp_path: Path):
    _write_transcript(tmp_path)
    with _patch_output(tmp_path):
        resp = client.get("/transcripts/session01/excerpt/SPEAKER%00_00")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Phase 1 audit fixes — F2 (template must not prefill raw labels)
# ---------------------------------------------------------------------------

def test_enroll_form_first_pass_leaves_input_empty(client: TestClient, tmp_path: Path):
    """F2 layer 1: on a first pass (body still has raw '**SPEAKER_00**'
    labels, no renames applied yet), the input must render empty rather than
    prefilled with the raw label -- prefilling it means submitting untouched
    fields creates junk 'SPEAKER_00' voice profiles."""
    _write_transcript(tmp_path)
    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}):
        resp = client.get("/transcripts/session01/enroll")
    body = resp.content.decode()
    assert 'name="speaker_SPEAKER_00"' in body
    assert 'value="SPEAKER_00"' not in body
    assert 'value="SPEAKER_01"' not in body
    # The raw label is still shown as a placeholder/heading, not lost entirely
    assert "SPEAKER_00" in body


# ---------------------------------------------------------------------------
# Phase 1 audit fixes — F2 (refuse raw-label-shaped submissions)
# ---------------------------------------------------------------------------

def test_enroll_submit_refuses_raw_label_shaped_name(client: TestClient, tmp_path: Path):
    """Submitting a field whose value is still 'SPEAKER_05' (i.e. untouched)
    must not rename the transcript or enroll a profile."""
    md = tmp_path / "session01.md"
    original = (
        "---\ntitle: Session 01\nspeakers:\n- name: SPEAKER_00\n---\n\n"
        "**SPEAKER_00** *(00:00)*: Hello everyone\n"
    )
    md.write_text(original, encoding="utf-8")
    diar = {
        "input_path": "/tmp/session01.mp3",
        "campaign": None,
        "diarization_segments": [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
        ],
    }
    (tmp_path / "session01_diar.json").write_text(json.dumps(diar), encoding="utf-8")

    with _patch_output(tmp_path), \
         patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker") as mock_enroll:
        resp = client.post(
            "/transcripts/session01/enroll",
            data={"speaker_SPEAKER_00": "SPEAKER_00"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    mock_enroll.assert_not_called()
    assert md.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Phase 2.5 — enroll_shared.enroll_profiles() unit tests
#
# The slow embedding-extraction logic (F3's EMA merge, averaging across raw
# labels, campaign membership) moved out of the synchronous HTTP request into
# enroll_profiles(), called from the JOB_ENROLL job runner. These tests
# exercise that function directly instead of through a full HTTP round trip,
# since the route no longer runs it inline (see the JOB_ENROLL-specific
# runner tests in tests/test_web_jobs.py for the job-runner side).
# ---------------------------------------------------------------------------

def test_enroll_profiles_calls_enroll_speaker_for_new_profile(tmp_path: Path):
    from wisper_transcribe.models import DiarizationSegment
    from wisper_transcribe.web.enroll_shared import enroll_profiles

    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake-mp3")
    segments = [DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00")]

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker") as mock_enroll, \
         patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p):
        enroll_profiles(
            input_path=audio,
            segments=segments,
            groups={"Alice": ["SPEAKER_00"]},
            campaign_slug=None,
            device="cpu",
        )

    mock_enroll.assert_called_once()
    kw = mock_enroll.call_args.kwargs
    assert kw["display_name"] == "Alice"
    assert kw["speaker_label"] == "SPEAKER_00"
    assert kw["audio_path"] == audio


def test_enroll_profiles_existing_profile_uses_ema_update(tmp_path: Path):
    """F3: resubmitting a name that already has a voice profile must merge
    via update_embedding (EMA), never enroll_speaker (which overwrites)."""
    import numpy as np
    from wisper_transcribe.models import DiarizationSegment, SpeakerProfile
    from wisper_transcribe.web.enroll_shared import enroll_profiles

    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake-mp3")
    segments = [DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00")]

    existing_alice = SpeakerProfile(
        name="alice", display_name="Alice", role="", embedding_path=tmp_path / "alice.npy",
        enrolled_date="2026-01-01", enrollment_source="old.mp3",
    )
    new_emb = np.ones(4)

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={"alice": existing_alice}), \
         patch("wisper_transcribe.speaker_manager.extract_embedding", return_value=new_emb), \
         patch("wisper_transcribe.speaker_manager.update_embedding") as mock_update, \
         patch("wisper_transcribe.speaker_manager.enroll_speaker") as mock_enroll, \
         patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p):
        enroll_profiles(
            input_path=audio,
            segments=segments,
            groups={"Alice": ["SPEAKER_00"]},
            campaign_slug=None,
            device="cpu",
        )

    mock_enroll.assert_not_called()
    mock_update.assert_called_once()
    assert mock_update.call_args.args[0] == "alice"


def test_enroll_profiles_averages_two_labels_same_name(tmp_path: Path):
    """F3: two raw labels assigned the same display name in one submit must
    have their embeddings averaged before being saved, not overwritten by
    whichever label happens to process last."""
    import numpy as np
    from wisper_transcribe.models import DiarizationSegment
    from wisper_transcribe.web.enroll_shared import enroll_profiles

    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake-mp3")
    segments = [
        DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00"),
        DiarizationSegment(start=10.0, end=15.0, speaker="SPEAKER_01"),
    ]
    emb_a = np.array([1.0, 0.0, 0.0])
    emb_b = np.array([0.0, 1.0, 0.0])

    def fake_extract(audio_path, segs, label, device="cpu"):
        return emb_a if label == "SPEAKER_00" else emb_b

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.speaker_manager.extract_embedding", side_effect=fake_extract), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker") as mock_enroll, \
         patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p):
        enroll_profiles(
            input_path=audio,
            segments=segments,
            groups={"Alice": ["SPEAKER_00", "SPEAKER_01"]},
            campaign_slug=None,
            device="cpu",
        )

    mock_enroll.assert_called_once()
    kw = mock_enroll.call_args.kwargs
    assert kw["display_name"] == "Alice"
    np.testing.assert_array_almost_equal(kw["embedding"], (emb_a + emb_b) / 2)


def test_enroll_profiles_adds_to_campaign(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.campaign_manager import create_campaign, get_campaign_profile_keys
    from wisper_transcribe.models import DiarizationSegment
    from wisper_transcribe.web.enroll_shared import enroll_profiles

    create_campaign("D&D Mondays")
    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake")
    segments = [DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00")]

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker"), \
         patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p):
        enroll_profiles(
            input_path=audio,
            segments=segments,
            groups={"Alice": ["SPEAKER_00"]},
            campaign_slug="d-d-mondays",
            device="cpu",
        )

    assert "alice" in get_campaign_profile_keys("d-d-mondays")


def test_enroll_profiles_no_campaign_skips_add_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No campaign slug -> add_member is never called (normal enrollment path)."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.models import DiarizationSegment
    from wisper_transcribe.web.enroll_shared import enroll_profiles

    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake")
    segments = [DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00")]

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker"), \
         patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p), \
         patch("wisper_transcribe.campaign_manager.add_member") as mock_add_member:
        enroll_profiles(
            input_path=audio,
            segments=segments,
            groups={"Bob": ["SPEAKER_00"]},
            campaign_slug=None,
            device="cpu",
        )

    mock_add_member.assert_not_called()


def test_enroll_profiles_skips_add_member_if_already_in_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """If the profile is already a campaign member, add_member must not be
    called again (avoids clobbering role/character)."""
    monkeypatch.setenv("WISPER_DATA_DIR", str(tmp_path))
    from wisper_transcribe.campaign_manager import create_campaign, add_member
    from wisper_transcribe.models import DiarizationSegment
    from wisper_transcribe.web.enroll_shared import enroll_profiles

    create_campaign("D&D Mondays")
    add_member("d-d-mondays", "alice", role="player", character="Tika")

    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake")
    segments = [DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00")]

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker"), \
         patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p), \
         patch("wisper_transcribe.campaign_manager.add_member") as mock_add_member:
        enroll_profiles(
            input_path=audio,
            segments=segments,
            groups={"Alice": ["SPEAKER_00"]},
            campaign_slug="d-d-mondays",
            device="cpu",
        )

    mock_add_member.assert_not_called()


def test_enroll_profiles_calls_progress_callback(tmp_path: Path):
    """(f) enroll_profiles surfaces status lines via the progress callback,
    which the JOB_ENROLL runner wires to job.log_lines."""
    from wisper_transcribe.models import DiarizationSegment
    from wisper_transcribe.web.enroll_shared import enroll_profiles

    audio = tmp_path / "session01.mp3"
    audio.write_bytes(b"fake")
    segments = [DiarizationSegment(start=0.0, end=5.0, speaker="SPEAKER_00")]
    messages: list[str] = []

    with patch("wisper_transcribe.speaker_manager.load_profiles", return_value={}), \
         patch("wisper_transcribe.speaker_manager.enroll_speaker"), \
         patch("wisper_transcribe.audio_utils.convert_to_wav", side_effect=lambda p: p):
        enroll_profiles(
            input_path=audio,
            segments=segments,
            groups={"Alice": ["SPEAKER_00"]},
            campaign_slug=None,
            device="cpu",
            progress=messages.append,
        )

    assert any("Converting audio" in m for m in messages)
    assert any("Alice" in m for m in messages)


# ---------------------------------------------------------------------------
# Phase 2.5 — job_detail.html renders an "E" pill for enroll jobs
# ---------------------------------------------------------------------------

def test_job_detail_renders_enroll_step_pill(client: TestClient, tmp_path: Path):
    """(g) job_detail.html shows a single "E" / "Enroll" step pill for
    JOB_ENROLL jobs, the same treatment refine/summarize jobs already get.

    Inserted directly into the queue's job dict (not via submit_enroll, which
    would also enqueue it onto the live background worker -- the app's
    lifespan starts that worker, and it would race to actually process this
    job, e.g. failing it for a nonexistent sidecar, before the GET below."""
    from wisper_transcribe.web.jobs import Job, JOB_ENROLL, RUNNING
    from datetime import datetime
    import uuid

    job = Job(
        id=str(uuid.uuid4()),
        status=RUNNING,
        created_at=datetime.now(),
        input_path=str(tmp_path / "session01.md"),
        kwargs={},
        job_type=JOB_ENROLL,
        output_path=str(tmp_path / "session01.md"),
        enroll_md_path=str(tmp_path / "session01.md"),
        enroll_groups={"Alice": ["SPEAKER_00"]},
    )
    client.app.state.job_queue._jobs[job.id] = job

    resp = client.get(f"/transcribe/jobs/{job.id}")

    assert resp.status_code == 200
    body = resp.content.decode()
    assert 'id="step_enroll"' in body
    assert ">Enroll<" in body
    # Not the transcription-job step pills
    assert 'id="step_transcribe"' not in body
    assert 'id="step_diarize"' not in body


def test_job_detail_completed_enroll_shows_transcript_link_not_name_speakers(
    client: TestClient, tmp_path: Path
):
    """On completion, a JOB_ENROLL job's detail page shows "View transcript"
    (job.output_path is set at submit time -- see submit_enroll) and hides
    "Name speakers" (already gated on job_type == "transcription")."""
    from wisper_transcribe.web.jobs import Job, JOB_ENROLL, COMPLETED
    from datetime import datetime
    import uuid

    transcript = tmp_path / "session01.md"
    transcript.write_text("# Session 01", encoding="utf-8")

    job = Job(
        id=str(uuid.uuid4()),
        status=COMPLETED,
        created_at=datetime.now(),
        input_path=str(transcript),
        kwargs={},
        job_type=JOB_ENROLL,
        output_path=str(transcript),
        enroll_md_path=str(transcript),
        enroll_groups={"Alice": ["SPEAKER_00"]},
    )
    client.app.state.job_queue._jobs[job.id] = job

    resp = client.get(f"/transcribe/jobs/{job.id}")

    assert resp.status_code == 200
    body = resp.content.decode()
    assert "View transcript" in body
    assert "Name speakers" not in body
