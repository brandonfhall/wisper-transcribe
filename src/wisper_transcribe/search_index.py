"""Full-text search over transcripts and their session summaries.

The index is derived from the ``.md`` files and disposable: ``wisper db
reindex`` drops and rebuilds it, so a stale or damaged index is never data
loss. Tables (migrations v7 and v8):

- ``search_index_state``: one row per indexed file (``<stem>.md`` and, if it
  existed, ``<stem>.summary.md``) with the ``mtime_ns``/size it had when
  indexed. A transcript with no ``transcript`` row is unindexed or stale.
- ``search_blocks``: one row per speaker block or summary section. Its FK to
  the state row means deleting a transcript's state rows ("mark stale")
  removes its blocks too.
- ``search_fts``: contentless FTS5 over the block text (no second copy of the
  text), kept in step with ``search_blocks`` by a delete trigger.
- ``transcript_titles``: FTS5 over ``transcripts.stem``, kept in step by
  triggers on ``transcripts``, so titles never need reindexing.

Freshness:

- The app's own writes reindex at once (:func:`reindex_path`, called by
  ``transcript_store.save_transcript``/``save_summary`` and ``register``).
- Edits made elsewhere (Obsidian, sync) are caught by :func:`check_freshness`
  (run by reconcile) or when a result's file no longer matches its state row.
  Both only mark the transcript stale; the backfill worker reindexes it, so no
  request parses a transcript.
- The backfill worker indexes every present transcript without a state row,
  one transaction per transcript, so a large archive doesn't delay startup and
  an interrupted build resumes where it stopped.

Snippets come from the current ``.md`` (FTS5 can't make them without the
content), located by ``block_idx``.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Optional

from markupsafe import Markup, escape

from . import db

log = logging.getLogger(__name__)

KIND_TRANSCRIPT = "transcript"
KIND_SUMMARY = "summary"
KINDS = (KIND_TRANSCRIPT, KIND_SUMMARY)
# A match on the transcript's title (its stem, transcript_titles). Not a
# filter value: the "transcript" filter includes title matches.
KIND_TITLE = "title"
# Title matches rank above text matches: bm25 scores from two FTS tables
# aren't comparable, and naming an episode should find that episode first.
TITLE_BOOST = 1e6
SUMMARY_SUFFIX = ".summary.md"

PAGE_SIZE = 20
# Pause between transcripts in a backfill, so a capture hot-path write
# (which waits at most recording_manager.HOT_PATH_BUSY_MS, then drops) always
# finds the lock free between two index transactions.
BACKFILL_YIELD_S = 0.05
HITS_PER_TRANSCRIPT = 3
SNIPPET_CHARS = 240

# False in tests: the web lifespan must not start a thread that races the
# tests' own DB assertions. Tests call run_backfill() directly.
AUTOSTART_WORKER = True


class Block(NamedTuple):
    idx: int
    speaker: Optional[str]
    start_s: Optional[float]
    text: str


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _body(text: str) -> str:
    """``text`` without its YAML frontmatter, which is metadata (speaker list,
    model names), not something to search."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            return parts[2]
    return text


def transcript_blocks(text: str) -> list[Block]:
    """The searchable blocks of a transcript ``.md``, indexed like its
    ``#b-<index>`` anchors (see ``formatter.searchable_blocks``)."""
    from .formatter import searchable_blocks
    from .time_utils import parse_timestamp

    blocks = []
    for _, b in searchable_blocks(_body(text)):
        start = parse_timestamp(b["timestamp"]) if b["timestamp"] else None
        blocks.append(Block(b["index"], b["speaker"] or None, start, b["text"]))
    return blocks


def summary_sections(text: str) -> list[Block]:
    """A summary's sections: index 0 is everything before the first ``## ``
    heading (the title), then one per ``## `` heading, matching the summary
    page's ``#s-<index>`` anchors. Empty sections keep their index."""
    sections: list[list[str]] = [[]]
    for line in _body(text).splitlines():
        if line.startswith("## "):
            sections.append([])
        sections[-1].append(line)
    return [Block(i, None, None, "\n".join(lines).strip()) for i, lines in enumerate(sections)]


def _blocks_for(kind: str, text: str) -> list[Block]:
    blocks = transcript_blocks(text) if kind == KIND_TRANSCRIPT else summary_sections(text)
    return [b for b in blocks if b.text]


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

def _stat(path: Path) -> Optional[tuple[int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _paths(stem: str, output_dir: Optional[Path]) -> Optional[dict[str, Path]]:
    from .transcript_store import safe_path

    md = safe_path(stem, ".md", output_dir)
    summary = safe_path(stem, SUMMARY_SUFFIX, output_dir)
    if md is None or summary is None:
        return None
    return {KIND_TRANSCRIPT: md, KIND_SUMMARY: summary}


def reindex(stem: str, *, data_dir: Optional[Path] = None,
            output_dir: Optional[Path] = None) -> bool:
    """Index ``<stem>.md`` and its summary, replacing what was indexed before.

    One short transaction. Each file is stat'ed *before* it is read, and that
    stat is what's stored: a write landing in between leaves a mismatch that
    the next freshness check catches, never fresh-looking state over old
    blocks. Undecodable bytes are replaced, and an unreadable file is indexed
    as empty, so a bad file is indexed once instead of being retried forever.
    Returns False when the transcript isn't registered or its ``.md`` is gone.
    """
    from .transcript_store import nfc

    paths = _paths(stem, output_dir)
    if paths is None:
        return False
    stem = nfc(paths[KIND_TRANSCRIPT].stem)
    files: dict[str, tuple[tuple[int, int], list[Block]]] = {}
    for kind, path in paths.items():
        st = _stat(path)
        if st is None:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # Indexed as empty rather than retried forever (the progress
            # counter would never finish); a later change to it reindexes.
            log.warning("Search index: can't read %s (%s); indexing it as empty", path.name, exc)
            text = ""
        files[kind] = (st, _blocks_for(kind, text))
    if KIND_TRANSCRIPT not in files:
        return False

    with db.transaction(data_dir) as conn:
        row = conn.execute("SELECT id FROM transcripts WHERE stem = ?", (stem,)).fetchone()
        if row is None:
            return False
        tid = row[0]
        # Cascades to the old blocks, whose trigger clears their FTS rows.
        conn.execute("DELETE FROM search_index_state WHERE transcript_id = ?", (tid,))
        for kind, ((mtime_ns, size), blocks) in files.items():
            conn.execute(
                "INSERT INTO search_index_state (transcript_id, kind, indexed_mtime_ns, indexed_size) "
                "VALUES (?, ?, ?, ?)", (tid, kind, mtime_ns, size),
            )
            for b in blocks:
                block_id = conn.execute(
                    "INSERT INTO search_blocks (transcript_id, kind, block_idx, speaker, start_s) "
                    "VALUES (?, ?, ?, ?, ?) RETURNING id",
                    (tid, kind, b.idx, b.speaker, b.start_s),
                ).fetchone()[0]
                conn.execute("INSERT INTO search_fts (rowid, text) VALUES (?, ?)", (block_id, b.text))
    return True


def reindex_path(path: Path, data_dir: Optional[Path] = None) -> None:
    """Reindex the transcript a just-written ``.md`` or ``.summary.md`` belongs to.

    Does nothing for files outside the output root (``wisper fix`` on a file
    elsewhere, ``summarize --output``) or not registered. Never raises: a
    failed reindex leaves the old state row, whose stat no longer matches the
    file, so the next freshness check reindexes it.
    """
    from .path_utils import get_output_dir

    path = Path(path)
    try:
        output_dir = get_output_dir()
        if os.path.realpath(path.parent) != os.path.realpath(output_dir):
            return
        name = path.name
        if name.endswith(SUMMARY_SUFFIX):
            stem = name[: -len(SUMMARY_SUFFIX)]
        elif name.endswith(".md"):
            stem = name[: -len(".md")]
        else:
            return
        reindex(stem, data_dir=data_dir, output_dir=output_dir)
    except Exception:
        log.warning("Search reindex failed for %s", path.name, exc_info=True)


def mark_stale(conn: sqlite3.Connection, transcript_ids: Iterable[int]) -> None:
    """Drop the index for these transcripts (inside ``conn``'s transaction);
    the backfill worker rebuilds them."""
    conn.executemany("DELETE FROM search_index_state WHERE transcript_id = ?",
                     [(tid,) for tid in transcript_ids])


def check_freshness(output_dir: Optional[Path] = None, data_dir: Optional[Path] = None) -> int:
    """Mark stale every present transcript whose files changed since indexing.

    A file changed if its mtime or size differs from its state row, it was
    deleted, or a summary appeared that wasn't there when indexed. Only
    ``stat()`` calls; no file is read. Returns how many were marked.
    """
    if output_dir is None:
        from .path_utils import get_output_dir
        output_dir = get_output_dir()
    with db.connection(data_dir) as conn:
        rows = conn.execute(
            "SELECT t.id, t.stem, s.kind, s.indexed_mtime_ns, s.indexed_size "
            "FROM search_index_state s JOIN transcripts t ON t.id = s.transcript_id "
            "WHERE t.missing_since IS NULL"
        ).fetchall()
    indexed: dict[int, dict] = {}
    for r in rows:
        entry = indexed.setdefault(r["id"], {"stem": r["stem"], "kinds": {}})
        entry["kinds"][r["kind"]] = (r["indexed_mtime_ns"], r["indexed_size"])
    stale = []
    for tid, entry in indexed.items():
        paths = _paths(entry["stem"], output_dir)
        if paths is None:
            continue
        for kind, path in paths.items():
            if _stat(path) != entry["kinds"].get(kind):
                stale.append(tid)
                break
    if stale:
        with db.transaction(data_dir) as conn:
            mark_stale(conn, stale)
        log.info("Search index: %d transcript(s) changed on disk; reindexing", len(stale))
        request_backfill()
    return len(stale)


def progress(data_dir: Optional[Path] = None) -> tuple[int, int]:
    """``(indexed, total)`` over present transcripts, for "Indexing N of M"."""
    with db.connection(data_dir) as conn:
        row = conn.execute(
            "SELECT count(*), count(s.transcript_id) FROM transcripts t "
            "LEFT JOIN search_index_state s ON s.transcript_id = t.id AND s.kind = ? "
            "WHERE t.missing_since IS NULL", (KIND_TRANSCRIPT,),
        ).fetchone()
    return row[1], row[0]


def run_backfill(data_dir: Optional[Path] = None, output_dir: Optional[Path] = None, *,
                 report: Optional[Callable[[int, int], None]] = None,
                 stop: Optional[threading.Event] = None,
                 yield_s: float = 0.0) -> int:
    """Index every present transcript that has no index yet. Returns how many
    were indexed. ``report(done, todo)`` is called after each one; ``yield_s``
    is a pause between transcripts (the server's worker sets it)."""
    if output_dir is None:
        from .path_utils import get_output_dir
        output_dir = get_output_dir()
    with db.connection(data_dir) as conn:
        todo = [r[0] for r in conn.execute(
            "SELECT t.stem FROM transcripts t WHERE t.missing_since IS NULL AND NOT EXISTS "
            "(SELECT 1 FROM search_index_state s WHERE s.transcript_id = t.id AND s.kind = ?) "
            "ORDER BY t.id", (KIND_TRANSCRIPT,),
        )]
    done = 0
    for n, stem in enumerate(todo, 1):
        if stop is not None and stop.is_set():
            break
        if yield_s and n > 1:
            (stop or threading.Event()).wait(yield_s)
        try:
            if reindex(stem, data_dir=data_dir, output_dir=output_dir):
                done += 1
        except Exception:
            log.warning("Search index: could not index %s", stem, exc_info=True)
        if report is not None:
            report(n, len(todo))
    return done


def rebuild(data_dir: Optional[Path] = None, output_dir: Optional[Path] = None, *,
            report: Optional[Callable[[int, int], None]] = None) -> int:
    """Drop the whole index and rebuild it from the files (``wisper db reindex``)."""
    with db.transaction(data_dir) as conn:
        conn.execute("DELETE FROM search_index_state")
        conn.execute("INSERT INTO search_fts (search_fts) VALUES ('delete-all')")
        conn.execute("INSERT INTO transcript_titles (transcript_titles) VALUES ('rebuild')")
    done = run_backfill(data_dir, output_dir, report=report)
    with db.transaction(data_dir) as conn:
        conn.execute("INSERT INTO search_fts (search_fts) VALUES ('optimize')")
    return done


# ---------------------------------------------------------------------------
# Background backfill worker (web server)
# ---------------------------------------------------------------------------

class _Worker:
    def __init__(self) -> None:
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._stop.clear()
        self._wake.set()  # the initial build
        self._thread = threading.Thread(target=self._run, name="wisper-search-backfill", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait()
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                n = run_backfill(stop=self._stop, yield_s=BACKFILL_YIELD_S)
                if n:
                    log.info("Search index: indexed %d transcript(s)", n)
            except Exception:
                log.warning("Search backfill failed", exc_info=True)

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


_worker: Optional[_Worker] = None


def start_worker() -> None:
    """Start the backfill thread (server startup), unless tests disabled it."""
    global _worker
    if not AUTOSTART_WORKER or (_worker is not None and _worker.running):
        return
    _worker = _Worker()
    _worker.start()


def stop_worker() -> None:
    global _worker
    if _worker is not None:
        _worker.stop()
        _worker = None


def request_backfill() -> None:
    """Wake the backfill thread, if one runs in this process. Otherwise the
    stale transcripts are indexed by the next server start or CLI search."""
    if _worker is not None:
        _worker.wake()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(r'"([^"]*)"(\*?)|(\S+)')
_WORD_RE = re.compile(r"\w+")
_SUFFIXES = ("ing", "es", "ed", "s")


def _terms(query: str) -> list[tuple[str, bool]]:
    """``(text, prefix)`` for each word or ``"phrase"`` in the user's query.

    Everything is data: quotes delimit phrases, a trailing ``*`` marks a
    prefix search, and other FTS5 syntax (``-``, ``:``, ``NEAR``, ``OR``,
    parentheses) is plain text. Tokens with no word characters are dropped.
    """
    terms = []
    for m in _TOKEN_RE.finditer(query):
        if m.group(1) is not None:
            text, prefix = m.group(1), bool(m.group(2))
        else:
            raw = m.group(3)
            prefix = raw.endswith("*")
            text = raw.replace('"', "").rstrip("*")
        words = _WORD_RE.findall(text)
        if words:
            terms.append((" ".join(words), prefix))
    return terms


def build_match(query: str) -> Optional[str]:
    """An FTS5 ``MATCH`` expression that treats ``query`` as plain input, or
    None when it has nothing searchable. Every term is quoted, so it can't
    be a syntax error; terms are ANDed."""
    parts = [f'"{text}"' + ("*" if prefix else "") for text, prefix in _terms(query)]
    return " ".join(parts) or None


def highlight_pattern(query: str) -> Optional[re.Pattern]:
    """Approximate highlighting for porter-stemmed matches.

    Python can't reproduce the stemmer, so each query word is matched by
    prefix: drop a common suffix (``fights`` → ``fight``), then match
    ``\\b<prefix>\\w*`` case-insensitively. A hit with nothing highlighted is
    acceptable; matching itself is exact.
    """
    stems = set()
    for text, prefix in _terms(query):
        for word in text.split():
            word = word.lower()
            if not prefix:
                for suffix in _SUFFIXES:
                    if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                        word = word[: -len(suffix)]
                        break
            stems.add(word)
    if not stems:
        return None
    alternatives = "|".join(re.escape(s) for s in sorted(stems, key=len, reverse=True))
    return re.compile(rf"\b(?:{alternatives})\w*", re.IGNORECASE)


def _densest_match(text: str, pattern: re.Pattern, width: int) -> Optional[int]:
    """Start of the match that begins the window holding the most distinct
    query terms (earliest on a tie), so a phrase beats a stray common word."""
    matches = [(m.start(), m.group(0).lower()) for m in pattern.finditer(text)]
    best, best_count = None, 0
    reach = width * 2 // 3
    for i, (pos, _) in enumerate(matches):
        terms = set()
        for p, term in matches[i:]:  # in position order: stop past the window
            if p - pos > reach:
                break
            terms.add(term)
        count = len(terms)
        if count > best_count:
            best, best_count = pos, count
    return best


def snippet(text: str, pattern: Optional[re.Pattern], width: int = SNIPPET_CHARS) -> Markup:
    """An HTML-safe excerpt of ``text`` around the first match, with matches
    wrapped in ``<mark>``. Every piece of transcript text is escaped; only
    the ``<mark>`` tags are markup."""
    anchor = _densest_match(text, pattern, width) if pattern else None
    start = 0
    if anchor is not None and anchor > width // 3:
        start = text.rfind(" ", 0, anchor - width // 3) + 1
    end = min(len(text), start + width)
    if end < len(text):
        space = text.rfind(" ", start, end)
        end = space if space > start else end
    window = text[start:end]
    out = [Markup("…") if start > 0 else Markup("")]
    pos = 0
    for m in (pattern.finditer(window) if pattern else ()):
        out.append(escape(window[pos:m.start()]))
        out.append(Markup("<mark>") + escape(m.group(0)) + Markup("</mark>"))
        pos = m.end()
    out.append(escape(window[pos:]))
    if end < len(text):
        out.append(Markup("…"))
    return Markup("").join(out)


_MD_LINE_PREFIX_RE = re.compile(r"^\s*(?:#{1,6}\s+|[-*]\s+(?:\[[ xX]\]\s+)?|\d+\.\s+)", re.MULTILINE)
_MD_INLINE_RE = re.compile(r"\*\*|__|\[\[|\]\]")


def _plain(markdown_text: str) -> str:
    """Summary markdown as readable snippet text: no heading or list markers,
    bold, or ``[[wiki links]]``; lines joined with spaces."""
    text = _MD_INLINE_RE.sub("", _MD_LINE_PREFIX_RE.sub("", markdown_text))
    return " ".join(text.split())


@dataclass
class Hit:
    kind: str
    block_idx: int
    speaker: Optional[str]
    start_s: Optional[float]
    snippet: Markup = field(default_factory=Markup)

    @property
    def anchor(self) -> str:
        if self.kind == KIND_TITLE:
            return ""
        return f"b-{self.block_idx}" if self.kind == KIND_TRANSCRIPT else f"s-{self.block_idx}"

    @property
    def timestamp(self) -> str:
        if self.start_s is None:
            return ""
        from .time_utils import format_timestamp
        return format_timestamp(self.start_s)


@dataclass
class ResultGroup:
    stem: str
    campaign_slug: Optional[str]
    campaign_name: Optional[str]
    total_hits: int
    hits: list[Hit]
    stale: bool = False  # changed since indexing: no snippets, no anchors


@dataclass
class SearchPage:
    query: str
    groups: list[ResultGroup]
    page: int
    has_next: bool
    error: Optional[str] = None


SEARCH_ERROR = "Couldn't search for that. Try different words."


def search(query: str, *, campaign: Optional[str] = None, speaker: Optional[str] = None,
           kind: Optional[str] = None, page: int = 1, per_page: int = PAGE_SIZE,
           data_dir: Optional[Path] = None, output_dir: Optional[Path] = None) -> SearchPage:
    """Search the index. Results are grouped by transcript, best match first,
    with up to :data:`HITS_PER_TRANSCRIPT` hits each; paging is by transcript.

    ``campaign`` is a slug, ``speaker`` an exact display name (transcript
    blocks only), ``kind`` ``"transcript"`` or ``"summary"``. A match on a
    transcript's title is a ``"title"`` hit, ranked first; the speaker and
    summary filters leave titles out. Missing transcripts are left out. A
    result whose file changed since indexing is returned ``stale`` and queued
    for reindexing.
    """
    page = max(1, page)
    match = build_match(query)
    if match is None or (kind is not None and kind not in KINDS):
        return SearchPage(query, [], page, False)
    if output_dir is None:
        from .path_utils import get_output_dir
        output_dir = get_output_dir()

    filters, params = ["search_fts MATCH ?", "t.missing_since IS NULL"], [match]
    campaign_filter = "t.campaign_id = (SELECT id FROM campaigns WHERE slug = ?)"
    if kind:
        filters.append("b.kind = ?")
        params.append(kind)
    if speaker:
        filters.append("b.speaker = ?")
        params.append(speaker)
    if campaign:
        filters.append(campaign_filter)
        params.append(campaign)
    titles = ""
    if speaker is None and kind in (None, KIND_TRANSCRIPT):
        title_filters = ["transcript_titles MATCH ?", "t.missing_since IS NULL"]
        params.append(match)
        if campaign:
            title_filters.append(campaign_filter)
            params.append(campaign)
        titles = f"""
          UNION ALL
          SELECT t.id, '{KIND_TITLE}', 0, NULL, NULL, bm25(transcript_titles) - {TITLE_BOOST}
          FROM transcript_titles
          JOIN transcripts t ON t.id = transcript_titles.rowid
          WHERE {' AND '.join(title_filters)}"""
    # MATERIALIZED: flattened into the outer query, bm25() leaves the FTS
    # scan's context and SQLite rejects it once the filters add joins.
    sql = f"""
        WITH hits AS MATERIALIZED (
          SELECT b.transcript_id, b.kind, b.block_idx, b.speaker, b.start_s,
                 bm25(search_fts) AS score
          FROM search_fts
          JOIN search_blocks b ON b.id = search_fts.rowid
          JOIN transcripts t ON t.id = b.transcript_id
          WHERE {' AND '.join(filters)}{titles}
        ), groups AS (
          SELECT transcript_id, min(score) AS best, count(*) AS n FROM hits
          GROUP BY transcript_id ORDER BY best, transcript_id LIMIT ? OFFSET ?
        ), ranked AS (
          SELECT h.*, row_number() OVER (
                   PARTITION BY h.transcript_id ORDER BY h.score, h.kind DESC, h.block_idx) AS rn
          FROM hits h JOIN groups g USING (transcript_id)
        )
        SELECT r.transcript_id, r.kind, r.block_idx, r.speaker, r.start_s, g.n,
               t.stem, c.slug, c.display_name
        FROM ranked r
        JOIN groups g USING (transcript_id)
        JOIN transcripts t ON t.id = r.transcript_id
        LEFT JOIN campaigns c ON c.id = t.campaign_id
        WHERE r.rn <= ?
        ORDER BY g.best, r.transcript_id, r.rn
    """
    params += [per_page + 1, (page - 1) * per_page, HITS_PER_TRANSCRIPT]
    try:
        with db.connection(data_dir) as conn:
            rows = conn.execute(sql, params).fetchall()
            ids = sorted({r["transcript_id"] for r in rows})
            states = conn.execute(
                "SELECT transcript_id, kind, indexed_mtime_ns, indexed_size FROM search_index_state "
                f"WHERE transcript_id IN ({','.join('?' * len(ids))})", ids,
            ).fetchall() if ids else []
    except sqlite3.OperationalError:
        log.info("Search query rejected: %r", match)
        return SearchPage(query, [], page, False, error=SEARCH_ERROR)

    groups: dict[int, ResultGroup] = {}
    for r in rows:
        group = groups.get(r["transcript_id"])
        if group is None:
            if len(groups) == per_page:
                continue  # the extra group only says there's a next page
            group = groups[r["transcript_id"]] = ResultGroup(
                r["stem"], r["slug"], r["display_name"], r["n"], [])
        group.hits.append(Hit(r["kind"], r["block_idx"], r["speaker"], r["start_s"]))
    has_next = len({r["transcript_id"] for r in rows}) > per_page

    indexed = {(s["transcript_id"], s["kind"]): (s["indexed_mtime_ns"], s["indexed_size"])
               for s in states}
    pattern = highlight_pattern(query)
    stale_ids = []
    for tid, group in groups.items():
        paths = _paths(group.stem, output_dir)
        texts: dict[str, list[Block]] = {}
        for k in {h.kind for h in group.hits} - {KIND_TITLE}:
            path = paths[k] if paths else None
            st = _stat(path) if path else None
            if st is None or st != indexed.get((tid, k)):
                group.stale = True
                break
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                group.stale = True
                break
            # Re-check: a write between the stat and the read would pair new
            # text with the old block numbers.
            if _stat(path) != st:
                group.stale = True
                break
            texts[k] = transcript_blocks(text) if k == KIND_TRANSCRIPT else summary_sections(text)
        if group.stale:
            stale_ids.append(tid)
            continue
        for hit in group.hits:
            if hit.kind == KIND_TITLE:
                hit.snippet = snippet(group.stem, pattern)
                continue
            blocks = texts[hit.kind]
            if hit.block_idx >= len(blocks) or blocks[hit.block_idx].idx != hit.block_idx:
                group.stale = True
                break
            text = blocks[hit.block_idx].text
            hit.snippet = snippet(_plain(text) if hit.kind == KIND_SUMMARY else text, pattern)
        if group.stale:
            stale_ids.append(tid)
    if stale_ids:
        try:
            with db.transaction(data_dir) as conn:
                mark_stale(conn, stale_ids)
            request_backfill()
        except sqlite3.Error:
            log.warning("Could not mark changed transcripts for reindexing", exc_info=True)
    return SearchPage(query, list(groups.values()), page, has_next)


def speakers(data_dir: Optional[Path] = None) -> list[str]:
    """Distinct speaker names in the index, for the speaker filter."""
    with db.connection(data_dir) as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT speaker FROM search_blocks WHERE speaker IS NOT NULL ORDER BY speaker"
        )]
