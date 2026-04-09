from __future__ import annotations

import datetime
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from tqdm import tqdm

from .audio_utils import SUPPORTED_EXTENSIONS, convert_to_wav, get_duration, validate_audio
from .config import check_ffmpeg, get_device, get_hf_token, load_config
from .formatter import to_markdown
from .models import TranscriptionSegment
from .transcriber import transcribe


def _seconds_to_hhmmss(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h}:{m:02d}:{s:02d}"


_MAX_PLAYBACK_SECONDS = 10.0


def _play_excerpt(wav_path: Path, start: float, end: float) -> None:
    """Play up to _MAX_PLAYBACK_SECONDS of a WAV excerpt using ffplay.

    ffplay ships with ffmpeg, which is already a hard dependency, making this
    reliable on Windows, macOS, and Linux without extra Python audio packages.
    Warns (but doesn't abort) if playback fails.
    """
    import click

    duration = min(end - start, _MAX_PLAYBACK_SECONDS)
    click.echo("  [playing audio excerpt...]")
    try:
        subprocess.run(
            [
                "ffplay",
                "-nodisp",
                "-autoexit",
                "-loglevel", "quiet",
                "-ss", str(start),
                "-t", str(duration),
                str(wav_path),
            ],
            check=True,
        )
    except FileNotFoundError:
        click.echo("  [ffplay not found — skipping audio playback]")
    except subprocess.CalledProcessError:
        click.echo("  [audio playback failed — skipping]")


# ---------------------------------------------------------------------------
# Module-level worker functions for ProcessPoolExecutor (must be picklable).
# Each runs in its own subprocess with isolated module-level globals (_model,
# _pipeline), so loading both models concurrently is safe.
# ---------------------------------------------------------------------------

def _transcribe_worker(wav_path: Path, **kwargs) -> list[TranscriptionSegment]:
    """Subprocess entry point for transcription (parallel_stages mode)."""
    from .transcriber import transcribe as _transcribe
    return _transcribe(wav_path, **kwargs)


def _diarize_worker(wav_path: Path, **kwargs) -> list:
    """Subprocess entry point for diarization (parallel_stages mode)."""
    from .diarizer import diarize as _diarize
    return _diarize(wav_path, **kwargs)


def _run_parallel_transcribe_diarize(
    wav_path: Path,
    transcribe_kwargs: dict,
    diarize_kwargs: dict,
) -> tuple[list[TranscriptionSegment], list]:
    """Run transcription and diarization concurrently via ProcessPoolExecutor.

    Each subprocess gets its own copy of the module-level model globals
    (_model, _pipeline), so there are no thread-safety concerns. On Linux/Mac,
    fork-based process creation has minimal overhead. On Windows, spawn-based
    creation adds ~1–2 seconds of startup cost per file, which is acceptable
    for 1–3 hour recordings.

    Returns (transcription_segments, diarization_segments).
    """
    tqdm.write("  Running transcription and diarization concurrently")
    with ProcessPoolExecutor(max_workers=2) as executor:
        trans_future = executor.submit(_transcribe_worker, wav_path, **transcribe_kwargs)
        diar_future = executor.submit(_diarize_worker, wav_path, **diarize_kwargs)
        segments = trans_future.result()
        diarization = diar_future.result()
    return segments, diarization


def process_file(
    path: Path,
    output_dir: Optional[Path] = None,
    model_size: str = "medium",
    device: str = "auto",
    language: Optional[str] = "en",
    include_timestamps: bool = True,
    overwrite: bool = False,
    no_diarize: bool = False,
    num_speakers: Optional[int] = None,
    min_speakers: Optional[int] = None,
    max_speakers: Optional[int] = None,
    enroll_speakers: bool = False,
    play_audio: bool = False,
    compute_type: str = "auto",
    vad_filter: Optional[bool] = None,
    initial_prompt: Optional[str] = None,
    hotwords: Optional[list[str]] = None,
) -> Path:
    """Run the full pipeline on a single audio file. Returns path to output .md."""
    from .config import resolve_compute_type

    path = Path(path)
    config = load_config()

    if device == "auto":
        device = get_device()
    if model_size == "medium":
        model_size = config.get("model", "medium")
    if language == "en":
        language = config.get("language", "en")
    if compute_type == "auto":
        compute_type = config.get("compute_type", "auto")
    if vad_filter is None:
        vad_filter = config.get("vad_filter", True)
    if hotwords is None:
        config_hotwords = config.get("hotwords", [])
        hotwords = config_hotwords if config_hotwords else None

    use_mlx: str = config.get("use_mlx", "auto")
    parallel_stages: bool = config.get("parallel_stages", False)

    check_ffmpeg()
    validate_audio(path)

    out_dir = Path(output_dir) if output_dir else path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (path.stem + ".md")

    if out_path.exists() and not overwrite:
        tqdm.write(f"  Skipping {path.name} — already processed (use --overwrite to re-run)")
        return out_path

    resolved_ct = resolve_compute_type(compute_type, device)
    tqdm.write("")
    tqdm.write("─" * 60)
    tqdm.write(f"  Input  : {path}")
    tqdm.write(f"  Output : {out_path}")
    # Show MLX backend label when it will be used; otherwise show CTranslate2 compute type.
    from .transcriber import _is_mlx_available
    _will_use_mlx = device == "mps" and use_mlx != "false" and _is_mlx_available()
    if _will_use_mlx:
        tqdm.write(f"  Model  : {model_size} (mps, mlx)")
    else:
        tqdm.write(f"  Model  : {model_size} ({device}, {resolved_ct})")
    tqdm.write("─" * 60)

    wav_path = convert_to_wav(path)

    # Resolve the HF token before any model runs so we know whether diarization
    # is possible. This is needed up-front in the parallel path.
    hf_token = ""
    if not no_diarize:
        hf_token = get_hf_token(config)

    # Decide whether to run transcription + diarization concurrently.
    _run_parallel = parallel_stages and not no_diarize and bool(hf_token)

    diarization = None
    if _run_parallel:
        transcribe_kw = dict(
            model_size=model_size,
            device=device,
            language=language,
            compute_type=compute_type,
            vad_filter=vad_filter,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            use_mlx=use_mlx,
        )
        diarize_kw = dict(
            hf_token=hf_token,
            device=device,
            num_speakers=num_speakers,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
        )
        segments, diarization = _run_parallel_transcribe_diarize(
            wav_path, transcribe_kw, diarize_kw
        )
    else:
        segments: list[TranscriptionSegment] = transcribe(
            wav_path,
            model_size=model_size,
            device=device,
            language=language,
            compute_type=compute_type,
            vad_filter=vad_filter,
            initial_prompt=initial_prompt,
            hotwords=hotwords,
            use_mlx=use_mlx,
        )

    duration = get_duration(wav_path)
    aligned_segments = segments
    speaker_map: Optional[dict[str, str]] = None
    speaker_metadata: list[dict] = []

    if not no_diarize:
        from .aligner import align

        if diarization is None and hf_token:
            # Sequential path: diarize now (parallel path already populated diarization).
            from .diarizer import diarize
            diarization = diarize(
                wav_path,
                hf_token=hf_token,
                device=device,
                num_speakers=num_speakers,
                min_speakers=min_speakers,
                max_speakers=max_speakers,
            )

        if diarization is not None:
            aligned_segments = align(segments, diarization)

            unique_speakers = sorted(
                {seg.speaker for seg in aligned_segments if seg.speaker != "UNKNOWN"},
                key=lambda label: min(s.start for s in aligned_segments if s.speaker == label),
            )

            if enroll_speakers:
                from .speaker_manager import (
                    _cosine_similarity, enroll_speaker, extract_embedding, load_profiles
                )
                import numpy as np
                import click
                speaker_map = {}
                existing_profiles = load_profiles()
                # Pre-load enrolled embeddings once for the whole enrollment session
                enrolled_embeddings: dict[str, np.ndarray] = {}
                for pname, prof in existing_profiles.items():
                    if prof.embedding_path.exists():
                        enrolled_embeddings[pname] = np.load(str(prof.embedding_path))
                click.echo(f"\n  Found {len(unique_speakers)} speaker(s). Let's name them.")
                for i, label in enumerate(unique_speakers, 1):
                    # Show a sample line for this speaker
                    sample = next(
                        (s for s in aligned_segments if s.speaker == label and s.text.strip()),
                        None,
                    )
                    sample_ts = f"{int(sample.start // 60):02d}:{int(sample.start % 60):02d}" if sample else "??"
                    sample_text = sample.text.strip() if sample else "(no sample)"
                    click.echo(f"\n  Speaker {i} of {len(unique_speakers)} (heard at {sample_ts}):")
                    click.echo(f'    "{sample_text[:80]}"')

                    # Offer replay loop if --play-audio is set
                    if play_audio and sample:
                        _play_excerpt(wav_path, sample.start, sample.end)

                    # Show existing speakers ranked by voice similarity
                    if enrolled_embeddings:
                        # Try to score this speaker against enrolled profiles
                        ranked: list[tuple[str, float]] = []
                        try:
                            query_emb = extract_embedding(wav_path, diarization, label, device)
                            for pname, emb in enrolled_embeddings.items():
                                ranked.append((pname, _cosine_similarity(query_emb, emb)))
                            ranked.sort(key=lambda x: x[1], reverse=True)
                        except Exception:
                            # Fallback to alphabetical if extraction fails
                            ranked = [(pname, 0.0) for pname in sorted(enrolled_embeddings)]
                        ranked_names = [pname for pname, _ in ranked]

                        threshold = config.get("similarity_threshold", 0.65)
                        click.echo("  Existing speakers:")
                        for idx, (pname, score) in enumerate(ranked, 1):
                            p = existing_profiles[pname]
                            role_str = f" ({p.role})" if p.role else ""
                            score_str = f" — {score:.0%}" if score > 0 else ""
                            match_str = " ★" if score >= threshold else ""
                            click.echo(f"    {idx}. {p.display_name}{role_str}{score_str}{match_str}")
                        click.echo("  Enter a number to select, or type a new name.")

                    # Name prompt — loop to support 'r' replay and numeric selection
                    name = ""
                    while True:
                        prompt_hint = " (or 'r' to replay)" if play_audio and sample else ""
                        raw = click.prompt(f"  Who is this?{prompt_hint}").strip()
                        if play_audio and sample and raw.lower() == "r":
                            _play_excerpt(wav_path, sample.start, sample.end)
                            continue
                        if enrolled_embeddings and raw.isdigit():
                            idx = int(raw) - 1
                            if 0 <= idx < len(ranked_names):
                                name = existing_profiles[ranked_names[idx]].display_name
                                break
                            else:
                                click.echo(f"  Please enter a number between 1 and {len(ranked_names)}, or a name.")
                                continue
                        if raw:
                            name = raw
                            break

                    # If user picked an existing profile, skip re-enrollment
                    is_existing = name in {p.display_name for p in existing_profiles.values()}
                    if is_existing:
                        profile_key = next(k for k, p in existing_profiles.items() if p.display_name == name)
                        role = existing_profiles[profile_key].role
                        notes = ""
                        click.echo(f"  Using existing profile for {name}.")
                        if click.confirm(
                            f"  Add this episode's audio to improve future recognition of {name}?",
                            default=False,
                        ):
                            from .speaker_manager import update_embedding
                            try:
                                new_emb = extract_embedding(wav_path, diarization, label, device)
                                update_embedding(profile_key, new_emb)
                                click.echo(f"  Updated voice profile for {name}.")
                            except Exception as exc:
                                click.echo(f"  Could not update profile: {exc}")
                    else:
                        role = click.prompt("  Role (DM/Player/Guest, optional)", default="").strip()
                        notes = click.prompt("  Notes (optional)", default="").strip()
                        enroll_speaker(
                            name=name.lower().replace(" ", "_"),
                            display_name=name,
                            role=role,
                            audio_path=wav_path,
                            segments=diarization,
                            speaker_label=label,
                            device=device,
                            data_dir=None,
                            notes=notes,
                        )

                    speaker_map[label] = name
                    speaker_metadata.append({"name": name, "role": role})
                click.echo(f"\n  Enrolled {len(unique_speakers)} speakers.")
            else:
                from .speaker_manager import match_speakers
                matches = match_speakers(
                    audio_path=wav_path,
                    diarization_segments=diarization,
                    data_dir=None,
                    device=device,
                    threshold=config.get("similarity_threshold", 0.65),
                )
                if matches:
                    speaker_map = matches
                    tqdm.write("  Speaker matches:")
                    for label, name in sorted(matches.items()):
                        tqdm.write(f"    {label} → {name}")
                    # Build speaker metadata from matched names (deduplicated, preserving order)
                    seen: set[str] = set()
                    for name in matches.values():
                        if name not in seen:
                            seen.add(name)
                            speaker_metadata.append({"name": name, "role": ""})
                else:
                    # No profiles yet — use raw labels
                    speaker_map = {s: s for s in unique_speakers}
                    for label in unique_speakers:
                        speaker_metadata.append({"name": label, "role": ""})

    metadata = {
        "title": path.stem.replace("_", " ").replace("-", " ").title(),
        "source_file": path.name,
        "date_processed": datetime.date.today().isoformat(),
        "duration": _seconds_to_hhmmss(duration),
        "speakers": speaker_metadata,
    }

    content = to_markdown(
        aligned_segments,
        speaker_map=speaker_map,
        metadata=metadata,
        include_timestamps=include_timestamps,
    )

    out_path.write_text(content, encoding="utf-8")
    tqdm.write(f"  Wrote {out_path.name}")
    return out_path


def process_folder(
    folder: Path,
    output_dir: Optional[Path] = None,
    verbose: bool = False,
    workers: int = 1,
    **kwargs,
) -> tuple[list[Path], list[str]]:
    """Process all audio files in a folder.

    Returns (successful_paths, error_messages).
    Skips files whose .md output already exists unless overwrite=True is in kwargs.

    workers > 1 enables parallel processing via ProcessPoolExecutor.  Only
    supported when device resolves to "cpu" — GPU processing is single-worker
    because faster-whisper and pyannote are not thread-safe when sharing VRAM.
    """
    folder = Path(folder)
    audio_files = sorted(
        f for f in folder.iterdir() if f.suffix.lower() in SUPPORTED_EXTENSIONS
    )

    if not audio_files:
        return [], []

    # Resolve effective device and enforce CPU-only guard
    effective_device = kwargs.get("device", "auto")
    if effective_device == "auto":
        effective_device = get_device()
    if workers > 1 and effective_device != "cpu":
        tqdm.write(
            f"  WARNING: --workers={workers} is only supported on CPU. "
            f"GPU processing requires a single worker. Clamping to 1."
        )
        workers = 1

    # Interactive enrollment requires a TTY — incompatible with subprocesses
    if workers > 1 and kwargs.get("enroll_speakers", False):
        tqdm.write(
            "  WARNING: --enroll-speakers requires interactive input and cannot "
            "run in parallel workers. Clamping to 1."
        )
        workers = 1

    successes: list[Path] = []
    errors: list[str] = []

    if workers > 1:
        progress = tqdm(
            total=len(audio_files),
            desc="Folder Progress",
            unit="file",
            position=0,
            leave=True,
            bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
            dynamic_ncols=True,
        )
        with ProcessPoolExecutor(max_workers=workers) as executor:
            future_to_file = {
                executor.submit(process_file, f, output_dir=output_dir, **kwargs): f
                for f in audio_files
            }
            for future in as_completed(future_to_file):
                f = future_to_file[future]
                progress.set_description(f"Finished {f.name}")
                progress.update(1)
                try:
                    result = future.result()
                    successes.append(result)
                except Exception as exc:
                    errors.append(f"{f.name}: {exc}")
                    tqdm.write(f"  ERROR {f.name}: {exc}")
        progress.close()
    else:
        overwrite = kwargs.get("overwrite", False)
        out_base = Path(output_dir) if output_dir else folder

        progress = tqdm(
            audio_files,
            desc="Folder Progress",
            unit="file",
            position=0,
            leave=True,
            bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
            dynamic_ncols=True,
        )

        for f in progress:
            progress.set_description(f"Processing {f.name}")
            out_path = out_base / (f.stem + ".md")
            if out_path.exists() and not overwrite:
                tqdm.write(f"  Skipping {f.name} — already processed (use --overwrite to re-run)")
                continue
            try:
                result = process_file(f, output_dir=output_dir, **kwargs)
                successes.append(result)
            except Exception as exc:
                errors.append(f"{f.name}: {exc}")
                tqdm.write(f"  ERROR {f.name}: {exc}")

    return successes, errors
