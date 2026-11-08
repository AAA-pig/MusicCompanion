"""Behavioral music memory: play counters and session summaries in a local SQLite db.

Boundaries agreed in the plan:
- stores countable behaviour only: no audio, no lyrics, no fingerprints;
- owns its own database file and does not depend on another plugin;
- writes once per listening session (not once per track) so the log stays readable;
- `capture_gap` never reaches this module: a capture hiccup is not a listening choice.

The module is deliberately synchronous and dependency-free so it can be imported
on any platform, including where GSMTC or WASAPI are unavailable.
"""
from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

LISTENED = "listened"
SKIPPED = "skipped"
UNKNOWN = "unknown"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    album TEXT NOT NULL DEFAULT '',
    listened INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    played_seconds REAL NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (title, artist, album)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    track_count INTEGER NOT NULL,
    skip_count INTEGER NOT NULL,
    played_seconds REAL NOT NULL,
    top_artist TEXT,
    summary TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    text TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL UNIQUE,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions (started_at);
CREATE INDEX IF NOT EXISTS idx_notes_session ON session_notes (session_id);
"""


def _local_naive(value: datetime) -> datetime:
    """Normalise to local naive time so stored timestamps stay comparable."""
    if value.tzinfo is None:
        return value
    return value.astimezone().replace(tzinfo=None)


def _iso(value: datetime) -> str:
    return _local_naive(value).isoformat(timespec="seconds")


def _hhmm(value: datetime) -> str:
    return _local_naive(value).strftime("%H:%M")


@dataclass(frozen=True)
class PlayRecord:
    """One finished track worth of behaviour, as reported by the poller.

    `played_seconds` is how long the track actually advanced; `total_seconds` is
    the system-reported duration when Windows provides a timeline. Without a
    duration the outcome can only be `skipped` and `unknown`.
    """

    title: str
    started_at: datetime
    artist: Optional[str] = None
    album: Optional[str] = None
    played_seconds: float = 0.0
    total_seconds: Optional[float] = None
    outcome: Optional[str] = None

    @property
    def ended_at(self) -> datetime:
        return _local_naive(self.started_at) + timedelta(seconds=max(self.played_seconds, 0.0))

    def resolved_outcome(self, *, skip_max_seconds: float, listen_ratio: float) -> str:
        if self.outcome in (LISTENED, SKIPPED, UNKNOWN):
            return str(self.outcome)
        played = max(self.played_seconds, 0.0)
        if self.total_seconds and self.total_seconds > 0:
            if played >= self.total_seconds * listen_ratio:
                return LISTENED
        if played <= skip_max_seconds:
            return SKIPPED
        return UNKNOWN


class MusicMemory:
    """Store listening behaviour, and turn it into a few readable sentences."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        session_gap_seconds: float = 300.0,
        skip_max_seconds: float = 20.0,
        listen_ratio: float = 0.9,
    ) -> None:
        if session_gap_seconds <= 0:
            raise ValueError("session_gap_seconds must be positive")
        if not 0 <= listen_ratio <= 1:
            raise ValueError("listen_ratio must be within [0, 1]")
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.session_gap_seconds = float(session_gap_seconds)
        self.skip_max_seconds = float(skip_max_seconds)
        self.listen_ratio = float(listen_ratio)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._session: Optional[dict[str, Any]] = None

    # ---------------------------------------------------------------- writing

    def record(self, record: PlayRecord) -> list[str]:
        """Apply one play record; return summaries written because a session ended."""
        with self._lock:
            outcome = record.resolved_outcome(
                skip_max_seconds=self.skip_max_seconds,
                listen_ratio=self.listen_ratio,
            )
            self._update_track(record, outcome)
            written: list[str] = []
            if self._session is None:
                self._session = self._new_session(record, outcome)
            elif (record.started_at - self._session["ended_at"]).total_seconds() > self.session_gap_seconds:
                written = self._flush_session(self._session)
                self._session = self._new_session(record, outcome)
            else:
                self._absorb(self._session, record, outcome)
            return written

    def flush(self) -> list[str]:
        """Close the open session now. Safe to call when nothing is pending."""
        with self._lock:
            if self._session is None:
                return []
            session, self._session = self._session, None
            return self._flush_session(session)

    def _new_session(self, record: PlayRecord, outcome: str) -> dict[str, Any]:
        started = _local_naive(record.started_at)
        return {
            "started_at": started,
            "ended_at": record.ended_at,
            "tracks": [(record.title, (record.artist or "").strip(), outcome)],
            "played_seconds": max(record.played_seconds, 0.0),
            "skip_count": 1 if outcome == SKIPPED else 0,
        }

    def _absorb(self, session: dict[str, Any], record: PlayRecord, outcome: str) -> None:
        session["tracks"].append((record.title, (record.artist or "").strip(), outcome))
        session["played_seconds"] += max(record.played_seconds, 0.0)
        if outcome == SKIPPED:
            session["skip_count"] += 1
        ended = record.ended_at
        if ended > session["ended_at"]:
            session["ended_at"] = ended

    def _update_track(self, record: PlayRecord, outcome: str) -> None:
        title = record.title.strip()
        if not title:
            return
        artist = (record.artist or "").strip()
        album = (record.album or "").strip()
        moment = _iso(record.started_at)
        listened = 1 if outcome == LISTENED else 0
        skipped = 1 if outcome == SKIPPED else 0
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO tracks (title, artist, album, listened, skipped, played_seconds,
                                    first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (title, artist, album) DO UPDATE SET
                    listened = listened + excluded.listened,
                    skipped = skipped + excluded.skipped,
                    played_seconds = played_seconds + excluded.played_seconds,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    title,
                    artist,
                    album,
                    listened,
                    skipped,
                    max(record.played_seconds, 0.0),
                    moment,
                    moment,
                ),
            )
            self._conn.commit()

    def _flush_session(self, session: dict[str, Any]) -> list[str]:
        tracks: list[tuple[str, str, str]] = session["tracks"]
        if not tracks:
            return []
        texts = self._compose(session)
        if not texts:
            return []
        cursor = self._conn.execute(
            """
            INSERT INTO sessions (started_at, ended_at, track_count, skip_count,
                                  played_seconds, top_artist, summary, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _iso(session["started_at"]),
                _iso(session["ended_at"]),
                len(tracks),
                session["skip_count"],
                session["played_seconds"],
                self._top_artist(tracks),
                texts[0],
                _iso(datetime.now()),
            ),
        )
        session_id = int(cursor.lastrowid)
        for extra in texts[1:]:
            self._conn.execute(
                "INSERT INTO session_notes (session_id, text) VALUES (?, ?)",
                (session_id, extra),
            )
        self._conn.commit()
        return texts

    @staticmethod
    def _top_artist(tracks: list[tuple[str, str, str]]) -> Optional[str]:
        counts = Counter(artist for _, artist, _ in tracks if artist)
        if not counts:
            return None
        artist, times = counts.most_common(1)[0]
        return artist if times >= 2 else None

    @staticmethod
    def _compose(session: dict[str, Any]) -> list[str]:
        tracks: list[tuple[str, str, str]] = session["tracks"]
        span = f"{_hhmm(session['started_at'])}-{_hhmm(session['ended_at'])}"
        head = f"{span} 连听 {len(tracks)} 首"
        if session["skip_count"]:
            head += f"，中间跳过 {session['skip_count']} 首"
        artist = MusicMemory._top_artist(tracks)
        if artist:
            head += f"（{artist} 为主）"
        texts = [head]

        repeats = Counter(
            title for title, _, outcome in tracks if outcome == LISTENED and title.strip()
        )
        for title, times in repeats.most_common(2):
            if times >= 2:
                texts.append(f"《{title}》这一段里完整听了 {times} 次")
        return texts

    # ---------------------------------------------------------------- reading

    def favourite_tracks(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT title, artist, listened, skipped, played_seconds
                FROM tracks
                WHERE listened > 0
                ORDER BY listened DESC, played_seconds DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def skipped_tracks(self, *, min_skips: int = 2, limit: int = 10) -> list[dict[str, Any]]:
        """Tracks to keep out of proactive recommendations, not out of recall."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT title, artist, skipped, listened
                FROM tracks
                WHERE skipped >= ? AND skipped > listened
                ORDER BY skipped DESC
                LIMIT ?
                """,
                (min_skips, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_notes(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.started_at, s.ended_at, s.summary AS text
                FROM sessions s
                UNION ALL
                SELECT s.started_at, s.ended_at, n.text
                FROM session_notes n JOIN sessions s ON s.id = n.session_id
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def digest(self, *, limit: int = 4) -> str:
        """A short block meant for an LLM tool call, not for a human report."""
        notes = [row["text"] for row in self.recent_notes(limit=limit)]
        favourites = self.favourite_tracks(limit=3)
        skipped = self.skipped_tracks(limit=3)
        lines: list[str] = []
        if notes:
            lines.append("最近的听歌记录：" + "；".join(notes))
        if favourites:
            top = ", ".join(
                f"{row['title']}{(' - ' + row['artist']) if row['artist'] else ''}（{row['listened']} 次）"
                for row in favourites
            )
            lines.append("常听的：" + top)
        if skipped:
            lines.append("老被跳过的：" + ", ".join(row["title"] for row in skipped))
        return "\n".join(lines) if lines else "音乐记忆还是空的。"

    # ----------------------------------------------------------------- diary

    def build_diary(self, day: date | datetime | None = None) -> str:
        """Compose one day of listening into a paragraph and store it."""
        if day is None:
            day = datetime.now().date()
        elif isinstance(day, datetime):
            day = day.date()
        prefix = day.isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT started_at, ended_at, track_count, skip_count, summary
                FROM sessions WHERE started_at LIKE ? ORDER BY started_at
                """,
                (prefix + "%",),
            ).fetchall()
            tracks = self._conn.execute(
                """
                SELECT title, artist, listened, skipped FROM tracks
                WHERE last_seen_at LIKE ? AND listened > 0
                ORDER BY listened DESC, played_seconds DESC LIMIT 3
                """,
                (prefix + "%",),
            ).fetchall()

        if not rows:
            text = f"{day.month} 月 {day.day} 日这天没有留下听歌记录。"
        else:
            total = sum(row["track_count"] for row in rows)
            skips = sum(row["skip_count"] for row in rows)
            first, last = _hhmm(datetime.fromisoformat(rows[0]["started_at"])), _hhmm(
                datetime.fromisoformat(rows[-1]["ended_at"])
            )
            parts = [f"{day.month} 月 {day.day} 日一共听了 {total} 首，从 {first} 听到 {last}"]
            if skips:
                parts.append(f"中间切掉 {skips} 首")
            if tracks:
                top = "、".join(
                    f"《{row['title']}》" for row in tracks if row["listened"] > 0
                )
                if top:
                    parts.append(f"反复回去听的是 {top}")
            text = "，".join(parts) + "。"

        with self._lock:
            self._conn.execute(
                """
                INSERT INTO diary (day, text, created_at) VALUES (?, ?, ?)
                ON CONFLICT (day) DO UPDATE SET text = excluded.text, created_at = excluded.created_at
                """,
                (prefix, text, _iso(datetime.now())),
            )
            self._conn.commit()
        return text

    def diary(self, day: date | datetime | None = None) -> Optional[str]:
        if day is None:
            day = datetime.now().date()
        elif isinstance(day, datetime):
            day = day.date()
        with self._lock:
            row = self._conn.execute(
                "SELECT text FROM diary WHERE day = ?", (day.isoformat(),)
            ).fetchone()
        return row["text"] if row else None

    # -------------------------------------------------------------- lifecycle

    def close(self) -> None:
        with self._lock:
            self._session = None
            self._conn.close()

    def __enter__(self) -> "MusicMemory":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
