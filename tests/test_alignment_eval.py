"""Tests for scripts/alignment_eval.py's pure logic (arms, sheet, score)."""
import csv
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_spec = importlib.util.spec_from_file_location(
    "alignment_eval", Path(__file__).parents[1] / "scripts" / "alignment_eval.py"
)
ae = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ae)


def _eval_dir(tmp_path):
    """Four words. A speaks 0-2 s, B 2-4 s. Whisper puts "there" (really B's,
    aligned to 2.2-2.6) at 1.5-1.9, inside A's turn."""
    words = [
        {"text": "hi", "whisper": [0.2, 0.6], "aligned": [0.2, 0.6]},
        {"text": "so", "whisper": [0.8, 1.2], "aligned": [0.8, 1.2]},
        {"text": "there", "whisper": [1.5, 1.9], "aligned": [2.2, 2.6]},
        {"text": "friend", "whisper": [2.7, 3.2], "aligned": [2.7, 3.2]},
    ]
    turns = [[0.0, 2.0, "A"], [2.0, 4.0, "B"]]
    data = {"meta": {"start": 60.0}, "segments": [{"words": words}],
            "regular": turns, "exclusive": turns}
    (tmp_path / "eval.json").write_text(json.dumps(data))
    return tmp_path, data


def test_assign_uses_the_arms_timing(tmp_path):
    _, data = _eval_dir(tmp_path)
    assert ae.assign(data, "whisper")[0] == ["A", "A", "A", "B"]
    assert ae.assign(data, "aligned-nosmooth")[0] == ["A", "A", "B", "B"]


def test_sheet_covers_both_sides_of_every_change_and_is_blind(tmp_path):
    out, _ = _eval_dir(tmp_path)
    ae.cmd_sheet(SimpleNamespace(out=str(out), force=False))
    rows = list(csv.DictReader((out / "sheet.csv").open()))
    # "there" (index 2) is the only word the arms disagree on, so it's first.
    assert [int(r["word_index"]) for r in rows] == [2, 0, 1, 3]
    assert [r["discriminating"] for r in rows] == ["yes", "", "", ""]
    assert set(rows[0]) == {"word_index", "discriminating", "clip_time", "source_time", "word",
                            "context", "correct_speaker"}
    assert rows[0]["source_time"] == "1:02.20"  # clip start 60 s + aligned 2.2 s
    assert "A:" in (out / "speakers.txt").read_text()


def test_sheet_refuses_to_overwrite_labels(tmp_path):
    out, _ = _eval_dir(tmp_path)
    ae.cmd_sheet(SimpleNamespace(out=str(out), force=False))
    with pytest.raises(SystemExit):
        ae.cmd_sheet(SimpleNamespace(out=str(out), force=False))


def test_score_counts_accuracy_and_wins(tmp_path, capsys):
    out, _ = _eval_dir(tmp_path)
    ae.cmd_sheet(SimpleNamespace(out=str(out), force=False))
    rows = list(csv.DictReader((out / "sheet.csv").open()))
    truth = {"0": "A", "1": "A", "2": "B", "3": "?"}  # "?" is skipped
    for r in rows:
        r["correct_speaker"] = truth[r["word_index"]]
    with (out / "sheet.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)

    ae.cmd_score(SimpleNamespace(outs=[str(out)]))
    report = capsys.readouterr().out
    assert "whisper 2/3" in report
    assert "aligned-nosmooth 3/3" in report


def test_guard_arm_keeps_whisper_time_for_large_moves(tmp_path):
    _, data = _eval_dir(tmp_path)
    data["segments"][0]["words"][2]["aligned"] = [3.0, 3.4]  # moved 1.5 s
    assert ae.assign(data, "aligned")[0][2] == "B"
    assert ae.assign(data, "aligned-guard-1s")[0][2] == "A"  # Whisper's 1.5-1.9
