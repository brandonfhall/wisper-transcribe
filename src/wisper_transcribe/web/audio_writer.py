"""Segmented Opus-in-Ogg audio writer and real-time PCM mixer.

SegmentedOggWriter writes a stream of raw Opus packets into sequentially-
numbered, self-contained Ogg/Opus segment files, rotating at a configurable
duration boundary.  Each finalized segment has a proper EOS page and is
independently playable by ffmpeg/ffplay — satisfying v1 file-format
invariant 1 (self-contained containers) and invariant 3 (≤60 s segments).

RealtimeMixer mixes multiple per-user int16 PCM streams into a single
combined stream with saturation clipping at ±32767.

No Discord or Pycord dependencies; designed for use by BotManager (Phase 3).
"""
from __future__ import annotations

import random
import struct
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Ogg CRC-32
# Ogg uses a non-standard MSB-first CRC with polynomial 0x04C11DB7.
# ---------------------------------------------------------------------------


def _make_ogg_crc_table() -> list[int]:
    table = []
    for i in range(256):
        crc = i << 24
        for _ in range(8):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
            else:
                crc = (crc << 1) & 0xFFFFFFFF
        table.append(crc)
    return table


_OGG_CRC_TABLE: list[int] = _make_ogg_crc_table()


def _ogg_crc32(data: bytes) -> int:
    """Compute the Ogg CRC-32 checksum over a complete page (checksum field zeroed)."""
    crc = 0
    for byte in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_CRC_TABLE[((crc >> 24) ^ byte) & 0xFF]
    return crc


# ---------------------------------------------------------------------------
# Low-level Ogg stream writer (one logical bitstream = one segment file)
# ---------------------------------------------------------------------------

_OPUS_SAMPLES_PER_PACKET = 960  # 20 ms @ 48 kHz — standard Discord Opus frame


class _OggStreamWriter:
    """Write a single self-contained Ogg/Opus logical bitstream to *path*.

    Protocol:
    1. Construct — immediately writes the OpusHead (BOS) and OpusTags pages.
    2. Call write_packet() for each raw Opus packet.
    3. Call close() to flush the EOS page and close the file.

    The one-packet lookahead in write_packet() / close() guarantees the EOS
    flag is set on the last audio data page, which ffprobe requires for a
    well-formed Ogg/Opus stream.
    """

    def __init__(self, path: Path) -> None:
        self._f = open(path, "wb")
        self._serial = random.randint(0, 0xFFFF_FFFF)
        self._seq = 0
        self._granule = 0
        self._pending: Optional[bytes] = None  # one-packet lookahead for EOS
        self._closed = False
        self._write_headers()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write_packet(self, packet: bytes) -> None:
        """Enqueue one Opus packet; the previous packet is flushed to disk."""
        if self._pending is not None:
            self._granule += _OPUS_SAMPLES_PER_PACKET
            self._write_page([self._pending], eos=False)
        self._pending = packet

    def close(self) -> None:
        """Write the final packet with the EOS flag and close the file."""
        if self._closed:
            return
        if self._pending is not None:
            self._granule += _OPUS_SAMPLES_PER_PACKET
            self._write_page([self._pending], eos=True)
        else:
            # Edge case: no audio written — emit a minimal EOS page
            self._write_page([], eos=True)
        self._f.flush()
        self._f.close()
        self._closed = True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_headers(self) -> None:
        """Write OpusHead (BOS) and OpusTags pages per RFC 7845."""
        # § 5.1 OpusHead — 19 bytes, mono, 48 kHz, 312-sample pre-skip
        opus_head = struct.pack(
            "<8sBBHIhB",
            b"OpusHead",
            1,      # version
            1,      # channel count (mono)
            312,    # pre-skip (samples at 48 kHz)
            48000,  # input sample rate
            0,      # output gain (Q7.8 dB, 0 = unity)
            0,      # channel mapping family 0 (mono/stereo)
        )
        self._write_page([opus_head], bos=True)

        # § 5.2 OpusTags — vendor string only, zero user comments
        vendor = b"wisper-transcribe"
        opus_tags = (
            struct.pack("<8sI", b"OpusTags", len(vendor))
            + vendor
            + struct.pack("<I", 0)
        )
        self._write_page([opus_tags])

    def _build_segment_table(self, packets: list[bytes]) -> tuple[list[int], bytes]:
        """Build the Ogg lacing segment table for *packets*.

        Returns (segment_table, concatenated_data).  The table encodes each
        packet's length using Ogg's lacing rule: one 255-byte entry per full
        chunk, plus a final terminating entry in [0, 254].
        """
        data = b"".join(packets)
        segs: list[int] = []
        for pkt in packets:
            remaining = len(pkt)
            while remaining >= 255:
                segs.append(255)
                remaining -= 255
            segs.append(remaining)  # terminating lace value (may be 0)
        if not segs:
            segs = [0]  # minimal page for the empty-EOS edge case
        return segs, data

    def _write_page(
        self,
        packets: list[bytes],
        *,
        eos: bool = False,
        bos: bool = False,
    ) -> None:
        segs, data = self._build_segment_table(packets)

        header_type = 0
        if bos:
            header_type |= 0x02  # beginning of stream
        if eos:
            header_type |= 0x04  # end of stream

        # Ogg page header: capture_pattern(4) version(1) header_type(1)
        # granule_position(8) serial(4) page_seq(4) checksum(4) n_segs(1)
        page = struct.pack(
            "<4sBBqIII",
            b"OggS",
            0,              # stream structure version
            header_type,
            self._granule,
            self._serial,
            self._seq,
            0,              # checksum placeholder — patched after CRC
        )
        page += bytes([len(segs)]) + bytes(segs) + data

        # Checksum is at byte offset 22–25; patch it in after computing CRC
        crc = _ogg_crc32(page)
        page = page[:22] + struct.pack("<I", crc) + page[26:]

        self._f.write(page)
        self._seq += 1


# ---------------------------------------------------------------------------
# Segmented writer — rotates segment files at a configurable duration
# ---------------------------------------------------------------------------

_OPUS_FRAME_S = 0.020  # 20 ms per Discord Opus packet


class SegmentedOggWriter:
    """Write a continuous Opus stream into rotating numbered segment files.

    Each segment is written to:
        stream_dir / f"{index:04d}.opus"

    When the accumulated packet count reaches segment_duration_s / 20 ms,
    the current segment is closed (EOS page written) and a new one opened.

    Crash semantics: all segments before the current one are complete files.
    The in-progress segment will be missing its EOS page after a crash; at
    most one segment (≤ segment_duration_s seconds) is lost.  This satisfies
    v1 file-format invariants 1, 2, and 3.

    If stream_dir already contains .opus files when constructed (e.g. after a
    server restart), the writer resumes from the next available index so
    existing segments are never overwritten.

    Not thread-safe: designed for a single dedicated audio writer thread per
    stream.  Use separate instances for each Discord user track and the
    combined track.
    """

    def __init__(
        self,
        stream_dir: Path,
        segment_duration_s: float = 60.0,
    ) -> None:
        self._dir = Path(stream_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_packets = max(1, int(segment_duration_s / _OPUS_FRAME_S))

        # Resume after crash: start from the next index after any existing segments
        existing = sorted(self._dir.glob("*.opus"))
        self._index = len(existing)
        self._packet_count = 0
        self._writer: Optional[_OggStreamWriter] = None
        self._open_segment()

    def write(self, packet: bytes) -> None:
        """Write one Opus packet.  Rotates to a new segment at the duration boundary."""
        if self._packet_count >= self._max_packets:
            self._rotate()
        assert self._writer is not None
        self._writer.write_packet(packet)
        self._packet_count += 1

    def finalize(self) -> None:
        """Close the current segment with a proper EOS page.

        Must be called when recording stops.  After finalize(), write() must
        not be called again.
        """
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _segment_path(self, index: int) -> Path:
        return self._dir / f"{index:04d}.opus"

    def _open_segment(self) -> None:
        self._writer = _OggStreamWriter(self._segment_path(self._index))
        self._packet_count = 0

    def _rotate(self) -> None:
        """Finalize the current segment and open the next one."""
        if self._writer is not None:
            self._writer.close()
        self._index += 1
        self._open_segment()


# ---------------------------------------------------------------------------
# Real-time PCM mixer
# ---------------------------------------------------------------------------


class RealtimeMixer:
    """Mix multiple per-user int16 PCM streams into a single combined stream.

    Usage::

        mixer = RealtimeMixer()
        combined = mixer.mix({"alice": pcm_alice, "bob": pcm_bob})

    PCM format: signed 16-bit little-endian samples, 48 kHz mono.
    All streams must have the same byte length.  Samples that would exceed the
    int16 range are saturated at ±32767 rather than wrapping.
    """

    def mix(self, streams: dict[str, bytes]) -> bytes:
        """Mix *streams* and return the combined PCM bytes.

        Returns b"" when *streams* is empty.  Truncates to the shortest
        stream before mixing if lengths differ.
        """
        if not streams:
            return b""

        arrays = list(streams.values())
        n_bytes = min(len(a) for a in arrays)
        n_samples = n_bytes // 2  # 2 bytes per int16 sample

        if n_samples == 0:
            return b""

        # Decode all streams and accumulate into int sums (no overflow)
        mixed = [0] * n_samples
        fmt = f"<{n_samples}h"
        for pcm in arrays:
            samples = struct.unpack(fmt, pcm[:n_bytes])
            for i, s in enumerate(samples):
                mixed[i] += s

        # Saturate at int16 bounds and re-encode
        clamped = [max(-32768, min(32767, v)) for v in mixed]
        return struct.pack(fmt, *clamped)
