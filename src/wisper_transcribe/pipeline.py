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
from .time_utils import format_duration
from .transcriber import transcribe


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

def _patch_tqdm_for_queue(queue, channel: str) -> None:  # type: ignore[type-arg]
    """Patch tqdm in a subprocess to forward output to a multiprocessing Queue.

    Must be called before any ML library imports in the subprocess.

    Queue tuple format: (channel, msg_type, message)
      msg_type="log"  — tqdm.write() status messages; parent forwards via tqdm.write()
                        so they reach the debug log file if active.
      msg_type="bar"  — progress bar renders (last non-empty frame per update);
                        parent writes these directly to sys.stderr so they give
                        terminal progress without appearing in the log file.
    """
    import re as _re
    import tqdm as _tqdm_mod

    _ansi = _re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
    _orig_init = _tqdm_mod.tqdm.__init__

    def _write(msg: str, *a, **kw) -> None:
        if msg.strip():
            queue.put((channel, "log", msg.strip()))

    class _QueueFile:
        """Captures bar renders and forwards the last non-empty frame per update."""
        def write(self, s: str) -> None:
            clean = _ansi.sub('', s)
            # Each tqdm update may contain multiple \r-separated frames; take the last.
            parts = [p.strip() for p in clean.split('\r') if p.strip()]
            if parts:
                final_msg = parts[-1]
                # Strip trailing numeric/whitespace residue (e.g., "###5" -> "###")
                final_msg = _re.sub(r'[\s\d]+$', '', final_msg)
                queue.put((channel, "bar", final_msg))
        def flush(self) -> None:
            pass

    def _init(self, *a, **kw) -> None:  # type: ignore[misc]
        kw["file"] = _QueueFile()
        kw["dynamic_ncols"] = False
        kw["ncols"] = 80
        _orig_init(self, *a, **kw)

    _tqdm_mod.tqdm.write = _write  # type: ignore[method-assign]
    _tqdm_mod.tqdm.__init__ = _init  # type: ignore[method-assign]


def _transcribe_worker(wav_path: Path, _progress_queue=None, **kwargs) -> list[TranscriptionSegment]:
    """Subprocess entry point for transcription (parallel_stages mode)."""
    if _progress_queue is not None:
        _patch_tqdm_for_queue(_progress_queue, "transcribe")
    from .transcriber import transcribe as _transcribe
    return _transcribe(wav_path, **kwargs)


def _diarize_worker(wav_path: Path, _progress_queue=None, **kwargs) -> list:
    """Subprocess entry point for diarization (parallel_stages mode)."""
    # Suppress Lightning/pyannote noise before any ML import so there is
    # no gap in a freshly-spawned subprocess where warnings can leak.
    from ._noise_suppress import suppress_third_party_noise
    suppress_third_party_noise()
    if _progress_queue is not None:
        _patch_tqdm_for_queue(_progress_queue, "diarize")
    from .diarizer import diarize as _diarize
    return _diarize(wav_path, **kwargs)


def _run_parallel_transcribe_diarize(
    wav_path: Path,
    transcribe_kwargs: dict,
    diarize_kwargs: dict,
) -> tuple[list[TranscriptionSegment], list]:
    """Run transcription and diarization concurrently via ProcessPoolExecutor.

    Each subprocess gets its own copy of the module-level model globals
    (_model, _pipeline), so there are no thread-safety concerns. Progress
    from each subprocess is forwarded to the parent via a multiprocessing
    Queue and drained by a background thread that calls tqdm.write() with
    channel-prefixed messages so jobs._run_job captures them.

    Returns (transcription_segments, diarization_segments).
    """
    import multiprocessing
    import queue as _queue_mod
    import threading

    tqdm.write("  Running transcription and diarization concurrently")

    # On macOS (spawn start method) a plain multiprocessing.Queue cannot be
    # pickled and passed via ProcessPoolExecutor.submit().  A Manager-backed
    # queue is a proxy object that survives the pickle round-trip.
    with multiprocessing.Manager() as manager:
        mp_queue = manager.Queue()  # type: ignore[type-arg]
        _stop = threading.Event()

        # Track the last bar render per channel to suppress duplicate frames.
        _last_bar: dict[str, str] = {}

        def _drain() -> None:
            import sys
            while not _stop.is_set() or not mp_queue.empty():
                try:
                    channel, msg_type, msg = mp_queue.get(timeout=0.05)
                except _queue_mod.Empty:
                    continue
                except Exception:
                    break
                if msg_type == "log":
                    # Goes through tqdm.write → captured by debug log tee if active.
                    tqdm.write(msg)
                else:
                    # Bar render: write directly to stderr with \r so it updates
                    # in-place like a real tqdm bar.  Bypasses tqdm.write so it
                    # is NOT captured by the debug log tee (stays out of the file).
                    # Deduplicate to avoid redundant redraws.
                    if _last_bar.get(channel) != msg:
                        _last_bar[channel] = msg
                        sys.stderr.write(f"\r{msg}")
                        sys.stderr.flush()

        drain_thread = threading.Thread(target=_drain, daemon=True)
        drain_thread.start()
        try:
            with ProcessPoolExecutor(max_workers=2) as executor:
                trans_future = executor.submit(
                    _transcribe_worker, wav_path, _progress_queue=mp_queue, **transcribe_kwargs
                )
                diar_future = executor.submit(
                    _diarize_worker, wav_path, _progress_queue=mp_queue, **diarize_kwargs
                )
                segments = trans_future.result()
                diarization = diar_future.result()
        finally:
            _stop.set()
            drain_thread.join(timeout=2.0)
            # Move cursor to a fresh line after the last in-place bar render.
            if _last_bar:
                import sys
                sys.stderr.write("\n")
                sys.stderr.flush()

    return segments, diarization


def _interactive_enroll(
    wav_path: Path,
    aligned_segments: list,
    diarization: list,
    unique_speakers: list[str],
    device: str,
    play_audio: bool,
    similarity_threshold: float,
) -> tuple[dict[str, str], list[dict]]:
    """Run the interactive speaker enrollment wizard.

    Presents each detected speaker with a sample utterance, optional audio
    playback, and similarity-ranked existing profiles.  Returns
    ``(speaker_map, speaker_metadata)`` for use by the formatter.
    """
    from .speaker_manager import (
        _cosine_similarity, enroll_speaker, extract_embedding, load_profiles, update_embedding,
    )
    import numpy as np
    import click

    speaker_map: dict[str, str] = {}
    speaker_metadata: list[dict] = []

    existing_profiles = load_profiles()
    enrolled_embeddings: dict[str, np.ndarray] = {}
    for pname, prof in existing_profiles.items():
        if prof.embedding_path.exists():
            enrolled_embeddings[pname] = np.load(str(prof.embedding_path))

    click.echo(f"\n  Found {len(unique_speakers)} speaker(s). Let's name them.")

    for i, label in enumerate(unique_speakers, 1):
        sample = next(
            (s for s in aligned_segments if s.speaker == label and s.text.strip()),
            None,
        )
        sample_ts = f"{int(sample.start // 60):02d}:{int(sample.start % 60):02d}" if sample else "??"
        sample_text = sample.text.strip() if sample else "(no sample)"
        click.echo(f"\n  Speaker {i} of {len(unique_speakers)} (heard at {sample_ts}):")
        click.echo(f'    "{sample_text[:80]}"')

        if play_audio and sample:
            _play_excerpt(wav_path, sample.start, sample.end)

        # Show existing speakers ranked by voice similarity
        ranked_names: list[str] = []
        if enrolled_embeddings:
            ranked: list[tuple[str, float]] = []
            try:
                query_emb = extract_embedding(wav_path, diarization, label, device)
                for pname, emb in enrolled_embeddings.items():
                    ranked.append((pname, _cosine_similarity(query_emb, emb)))
                ranked.sort(key=lambda x: x[1], reverse=True)
            except Exception:
                ranked = [(pname, 0.0) for pname in sorted(enrolled_embeddings)]
            ranked_names = [pname for pname, _ in ranked]

            click.echo("  Existing speakers:")
            for idx, (pname, score) in enumerate(ranked, 1):
                p = existing_profiles[pname]
                role_str = f" ({p.role})" if p.role else ""
                score_str = f" — {score:.0%}" if score > 0 else ""
                match_str = " ★" if score >= similarity_threshold else ""
                click.echo(f"    {idx}. {p.display_name}{role_str}{score_str}{match_str}")
            click.echo("  Enter a number to select, or type a new name.")

        # Name prompt — supports 'r' replay and numeric selection
        name = _prompt_speaker_name(
            enrolled_embeddings, ranked_names, existing_profiles,
            play_audio, sample, wav_path,
        )

        # If user picked an existing profile, skip re-enrollment
        is_existing = name in {p.display_name for p in existing_profiles.values()}
        if is_existing:
            profile_key = next(k for k, p in existing_profiles.items() if p.display_name == name)
            role = existing_profiles[profile_key].role
            click.echo(f"  Using existing profile for {name}.")
            if click.confirm(
                f"  Add this episode's audio to improve future recognition of {name}?",
                default=False,
            ):
                try:
                    new_emb = extract_embedding(wav_path, diarization, label, device)
                    update_embedding(profile_key, new_emb)
                    click.echo(f"  Updated voice profile for {name}.")
                except Exception as exc:
                    click.echo(f"  Could not update profile: {exc}")
        else:
            role = click.prompt("  Role (DM/Player/Guest, optional)", default="").strip()
            notes = click.prompt("  Notes (optional)", default="").strip()
            new_profile = enroll_speaker(
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
            # Refresh in-memory dicts so subsequent speakers in this file see
            # the new enrollment in the ranked candidates list.
            existing_profiles[new_profile.name] = new_profile
            try:
                enrolled_embeddings[new_profile.name] = np.load(str(new_profile.embedding_path))
            except Exception:
                pass

        speaker_map[label] = name
        speaker_metadata.append({"name": name, "role": role})

    click.echo(f"\n  Enrolled {len(unique_speakers)} speakers.")
    return speaker_map, speaker_metadata


def _prompt_speaker_name(
    enrolled_embeddings: dict,
    ranked_names: list[str],
    existing_profiles: dict,
    play_audio: bool,
    sample,
    wav_path: Path,
) -> str:
    """Prompt the user to name a speaker, supporting replay and numeric selection."""
    import click

    while True:
        prompt_hint = " (or 'r' to replay)" if play_audio and sample else ""
        raw = click.prompt(f"  Who is this?{prompt_hint}").strip()
        if play_audio and sample and raw.lower() == "r":
            _play_excerpt(wav_path, sample.start, sample.end)
            continue
        if enrolled_embeddings and raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(ranked_names):
                return existing_profiles[ranked_names[idx]].display_name
            else:
                click.echo(f"  Please enter a number between 1 and {len(ranked_names)}, or a name.")
                continue
        if raw:
            return raw


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
    campaign: Optional[str] = None,
    job_id: Optional[str] = None,
    _result_store: Optional[dict] = None,
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
            if _result_store is not None:
                _result_store["diarization_segments"] = list(diarization)
            aligned_segments = align(segments, diarization)

            unique_speakers = sorted(
                {seg.speaker for seg in aligned_segments if seg.speaker != "UNKNOWN"},
                key=lambda label: min(s.start for s in aligned_segments if s.speaker == label),
            )

            if enroll_speakers:
                speaker_map, speaker_metadata = _interactive_enroll(
                    wav_path=wav_path,
                    aligned_segments=aligned_segments,
                    diarization=diarization,
                    unique_speakers=unique_speakers,
                    device=device,
                    play_audio=play_audio,
                    similarity_threshold=config.get("similarity_threshold", 0.65),
                )
            else:
                from .speaker_manager import match_speakers
                profile_filter: Optional[set] = None
                if campaign:
                    from .campaign_manager import get_campaign_profile_keys
                    profile_filter = get_campaign_profile_keys(campaign)
                    tqdm.write(f"  Campaign filter: {campaign} ({len(profile_filter)} member(s))")
                matches = match_speakers(
                    audio_path=wav_path,
                    diarization_segments=diarization,
                    data_dir=None,
                    device=device,
                    threshold=config.get("similarity_threshold", 0.65),
                    profile_filter=profile_filter,
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
        "duration": format_duration(duration),
        "speakers": speaker_metadata,
    }
    if job_id:
        metadata["job_id"] = job_id

    content = to_markdown(
        aligned_segments,
        speaker_map=speaker_map,
        metadata=metadata,
        include_timestamps=include_timestamps,
    )

    out_path.write_text(content, encoding="utf-8")
    tqdm.write(f"  Wrote {out_path.name}")

    # Associate transcript with campaign so the list view can group it.
    if campaign:
        try:
            from .campaign_manager import move_transcript_to_campaign
            move_transcript_to_campaign(out_path.stem, campaign)
        except Exception:
            pass  # Non-fatal — transcript is still written

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
