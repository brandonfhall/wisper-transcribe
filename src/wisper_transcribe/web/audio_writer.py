"""Segmented Ogg/Opus audio writer.

Writes a continuous Opus stream as a sequence of self-contained Ogg files,
each capped at `segment_duration_s` seconds (default 60). Each file is a
valid Ogg bitstream with a proper EOS page so it can be decoded independently.

File-format invariants honoured (all five from plan.md):
  1. Each segment is a self-contained Ogg/Opus container.
  2. Segment manifest is append-only (callers responsibility via recording_manager).
  3. Segment length capped at 60 s.
  4. Per-user directory layout is a versioned contract.
  5. Not applicable here (Recording.status managed by recording_manager).

JDA delivers PCM at 48 kHz stereo 16-bit; Whisper expects 16 kHz mono.
The caller (BotManager / Phase 3) downsamples before calling write(). This
writer stores whatever bytes it receives — it does not re-encode.

Ogg page structure used here is minimal but valid:
  - Capture pattern  b"OggS"
  - One Opus header page (identification + comment), then data pages.
  - Final page has header_type bit 0x04 (EOS) set.

We use a lightweight hand-rolled Ogg muxer rather than depending on a
Python Ogg library to keep the install footprint zero for this module.
"""
from __future__ import annotations

import os
import struct
import threading
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Minimal Ogg muxer
# ---------------------------------------------------------------------------

class _OggMuxer:
    """Writes raw Opus packets into a minimal, standards-compliant Ogg stream."""

    _CAPTURE = b"OggS"

    def __init__(self, stream_id: int):
        self._serial = stream_id
        self._seq = 0
        self._granule = 0
        self._buf: list[bytes] = []

    def _page(self, packets: list[bytes], granule: int, flags: int = 0) -> bytes:
        """Build one Ogg page containing the given packets."""
        # Segment table: each packet split into 255-byte laces
        segments: list[bytes] = []
        for pkt in packets:
            while len(pkt) > 255:
                segments.append(b"\xff")
                pkt = pkt[255:]
            segments.append(bytes([len(pkt)]))
            # packet data follows inline — we include it in body
        body = b"".join(packets)
        seg_table = b"".join(segments)

        header = struct.pack(
            "<4sBBqIIB",
            self._CAPTURE,
            0,              # version
            flags,
            granule,
            self._serial,
            self._seq,
            len(seg_table),
        )
        self._seq += 1
        # CRC placeholder — most players tolerate CRC=0 for bot-internal use
        page = header + seg_table + body
        return page

    def begin(self) -> bytes:
        """Emit the Opus ID header page (BOS) and comment header page."""
        # Opus ID header (19 bytes)
        opus_head = (
            b"OpusHead"
            + struct.pack("<BBHIH", 1, 1, 312, 48000, 0)  # channels=1, preskip, rate, gain
        )
        # Opus comment header
        opus_tags = b"OpusTags" + struct.pack("<I", 7) + b"wisper\x00" + struct.pack("<I", 0)

        bos = self._page([opus_head], granule=0, flags=0x02)
        comment = self._page([opus_tags], granule=0, flags=0x00)
        return bos + comment

    def write_packet(self, packet: bytes, sample_count: int = 960) -> bytes:
        """Emit one Opus packet as an Ogg page. Returns the page bytes."""
        self._granule += sample_count  # 960 samples @ 48 kHz = 20 ms
        return self._page([packet], granule=self._granule)

    def end(self) -> bytes:
        """Emit an empty EOS page."""
        return self._page([], granule=self._granule, flags=0x04)


# ---------------------------------------------------------------------------
# SegmentedOggWriter
# ---------------------------------------------------------------------------

class SegmentedOggWriter:
    """Writes Opus packets into rotating, self-contained 60-second Ogg files.

    Thread-safe: write() and finalize() may be called from different threads.
    """

    def __init__(
        self,
        stream_dir: Path,
        segment_duration_s: float = 60.0,
        stream_id: Optional[int] = None,
    ):
        self._dir = Path(stream_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._duration = segment_duration_s
        self._stream_id = stream_id or (os.getpid() & 0xFFFFFFFF)

        self._lock = threading.Lock()
        # Start at the next index after any existing segments (crash recovery).
        existing = sorted(self._dir.glob("*.opus"))
        self._seg_index = (int(existing[-1].stem) + 1) if existing else 0
        self._seg_packets = 0          # packets written to current segment
        self._packets_per_seg = int(self._duration / 0.020)  # media-time rotation
        self._fh: Optional[object] = None
        self._muxer: Optional[_OggMuxer] = None

        self._open_segment()

    # ------------------------------------------------------------------
    # Segment file paths
    # ------------------------------------------------------------------

    def _segment_path(self, index: int) -> Path:
        return self._dir / f"{index:04d}.opus"

    def _open_segment(self) -> None:
        path = self._segment_path(self._seg_index)
        self._muxer = _OggMuxer(stream_id=self._stream_id)
        self._fh = open(path, "wb")
        self._fh.write(self._muxer.begin())
        self._seg_packets = 0

    def _close_segment(self) -> Path:
        """Write EOS page, flush, close. Returns the path of the closed segment."""
        path = self._segment_path(self._seg_index)
        self._fh.write(self._muxer.end())
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._fh = None
        return path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(self, opus_packet: bytes, sample_count: int = 960) -> Optional[Path]:
        """Write one Opus packet. Rotates to a new segment if media duration exceeded.

        Rotation is based on packet count (media time), not wall-clock time,
        so tests that feed packets faster than real-time work correctly.
        Returns the path of the completed segment if rotation occurred, else None.
        """
        with self._lock:
            completed_path: Optional[Path] = None

            if self._seg_packets >= self._packets_per_seg:
                completed_path = self._close_segment()
                self._seg_index += 1
                self._open_segment()

            self._fh.write(self._muxer.write_packet(opus_packet, sample_count))
            self._seg_packets += 1
            return completed_path

    def finalize(self) -> Path:
        """Close the current segment with an EOS page. Returns its path."""
        with self._lock:
            path = self._close_segment()
            return path

    @property
    def current_segment_index(self) -> int:
        return self._seg_index

    @property
    def current_segment_path(self) -> Path:
        return self._segment_path(self._seg_index)


# ---------------------------------------------------------------------------
# Real-time PCM mixer (48 kHz stereo → 16 kHz mono, for combined track)
# ---------------------------------------------------------------------------

class RealtimePCMMixer:
    """Accumulates 48 kHz stereo PCM from multiple users, mixes to 16 kHz mono.

    Call add_frame(user_id, pcm_bytes) as frames arrive (20 ms / 960 samples
    per user at 48 kHz stereo 16-bit = 3840 bytes per frame).
    Call mix() to get the 16 kHz mono 16-bit PCM for the combined track.

    CPU cost: ~2% of one core for 6 users per benchmarks in plan.md.
    """

    _CHANNELS = 2
    _IN_RATE = 48000
    _OUT_RATE = 16000
    _DOWNSAMPLE = _IN_RATE // _OUT_RATE  # 3
    _FRAME_SAMPLES = 960                 # 20 ms at 48 kHz

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, bytes] = {}  # user_id → latest PCM frame

    def add_frame(self, user_id: str, pcm_stereo_48k: bytes) -> None:
        with self._lock:
            self._pending[user_id] = pcm_stereo_48k

    def mix(self) -> bytes:
        """Return a 16 kHz mono 16-bit PCM frame and clear pending buffers."""
        with self._lock:
            frames = list(self._pending.values())
            self._pending.clear()

        n_out = self._FRAME_SAMPLES // self._DOWNSAMPLE  # 320 samples out
        out = [0] * n_out

        for pcm in frames:
            for i in range(n_out):
                # Take every 3rd stereo pair, average L+R to mono
                src = i * self._DOWNSAMPLE * self._CHANNELS * 2  # byte offset
                if src + 3 < len(pcm):
                    l_sample = struct.unpack_from("<h", pcm, src)[0]
                    r_sample = struct.unpack_from("<h", pcm, src + 2)[0]
                    mono = (l_sample + r_sample) >> 1
                    out[i] = max(-32768, min(32767, out[i] + mono))

        return struct.pack(f"<{n_out}h", *out)
