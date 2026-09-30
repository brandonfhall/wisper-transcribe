"""Frozen copies of the pre-SQLite JSON serializers, for importer tests.

These write exactly what the JSON-era managers wrote, so importer tests don't
change when the managers do. Don't "fix" them to match current code.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np


def write_speakers(data_dir: Path, speakers: dict[str, dict],
                   embeddings: Optional[dict[str, np.ndarray]] = None) -> None:
    """``profiles/speakers.json`` plus ``profiles/embeddings/<key>.npy``.

    Each entry defaults to the JSON-era shape; pass fields to override or
    ``None`` to omit one.
    """
    profiles_dir = data_dir / "profiles"
    emb_dir = profiles_dir / "embeddings"
    emb_dir.mkdir(parents=True, exist_ok=True)
    raw = {}
    for key, fields in speakers.items():
        entry = {
            "display_name": key.title(),
            "role": "",
            "embedding_file": f"embeddings/{key}.npy",
            "enrolled_date": "2026-01-05",
            "enrollment_source": "session01.mp3",
            "notes": "",
            "embedding_space": "wespeaker-resnet34",
        }
        entry.update(fields)
        raw[key] = {k: v for k, v in entry.items() if v is not None}
    for key, vec in (embeddings or {}).items():
        np.save(str(emb_dir / f"{key}.npy"), np.asarray(vec, dtype=np.float32))
    (profiles_dir / "speakers.json").write_text(json.dumps(raw, indent=2), encoding="utf-8")


def write_campaigns(data_dir: Path, campaigns: dict[str, dict]) -> None:
    """``campaigns/campaigns.json``. Each value: display_name, created,
    members {key: {role, character, discord_user_id}}, transcripts [stems]."""
    path = data_dir / "campaigns" / "campaigns.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = {}
    for slug, c in campaigns.items():
        raw[slug] = {
            "display_name": c.get("display_name", slug.title()),
            "created": c.get("created", "2026-01-01"),
            "members": {
                key: {"role": m.get("role", ""), "character": m.get("character", ""),
                      "discord_user_id": m.get("discord_user_id")}
                for key, m in c.get("members", {}).items()
            },
            "transcripts": list(c.get("transcripts", [])),
        }
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
