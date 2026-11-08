"""Turn GSMTC snapshots into finished `PlayRecord`s.

The poller knows the current track; it does not know when a track ended. This
module keeps the small amount of state needed to answer that:

- the wall-clock moment a track started (back-dated by the reported position);
- how many seconds it was actually playing, ignoring paused stretches;
- the difference in reported position, which is more accurate than wall clock
  when Windows exposes a timeline.

The poller only emits on change, so a four-minute song usually produces two
snapshots: its start and the next track's start. Played time therefore comes
from wall clock by default, and `playback_status` is what excludes pauses.
`stale_after_seconds` is for machine sleep or a dead capture stream, not for a
paused song, so it defaults to half an hour.

Only completed tracks are handed to the memory layer. Nothing here reads audio,
guesses a position Windows did not report, or invents an outcome for a track
that was never finished.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from .music_memory import PlayRecord


def _local_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone().replace(tzinfo=None)


@dataclass(frozen=True)
class TrackObservation:
    observed_at: datetime
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    position_seconds: Optional[float] = None
    duration_seconds: Optional[float] = None
    playback_status: str = "playing"

    @property
    def identity(self) -> tuple[str, str, str]:
        return (
            (self.title or "").strip(),
            (self.artist or "").strip(),
            (self.album or "").strip(),
        )


class PlayLogRecorder:
    """Feed observations in, get `PlayRecord`s out through the memory sink.

    `memory` only needs a `record(PlayRecord) -> list[str]` method, so the
    recorder stays testable without a database.
    """

    def __init__(
        self,
        memory: Any,
        *,
        stale_after_seconds: float = 1800.0,
        max_played_seconds: float = 3600.0,
        max_step_seconds: float = 600.0,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self.memory = memory
        self.stale_after_seconds = float(stale_after_seconds)
        self.max_played_seconds = float(max_played_seconds)
        self.max_step_seconds = float(max_step_seconds)
        self._current: Optional[dict[str, Any]] = None

    @property
    def current_title(self) -> Optional[str]:
        return self._current["title"] if self._current else None

    def observe(self, observation: TrackObservation) -> list[str]:
        title, artist, album = observation.identity
        if not title and not artist:
            return []

        observed_at = _local_naive(observation.observed_at)
        if self._current is None:
            self._start(observation)
            return []

        gap = (observed_at - self._current["last_observed_at"]).total_seconds()
        if observation.identity != self._current["identity"]:
            written = self._finalize()
            self._start(observation)
            return written
        if gap > self.stale_after_seconds:
            # He left for a while mid-track: close the listening session too.
            written = self.flush()
            self._start(observation)
            return written

        self._current["playing_seconds"] += self._step_seconds(
            observation, gap
        )
        self._current["last_observed_at"] = observed_at
        self._current["last_position"] = observation.position_seconds
        if observation.duration_seconds:
            self._current["duration_seconds"] = observation.duration_seconds
        self._current["playback_status"] = observation.playback_status
        return []

    def flush(self) -> list[str]:
        """Close the track in flight and the open listening session."""
        written = self._finalize()
        sink_flush = getattr(self.memory, "flush", None)
        if callable(sink_flush):
            written = written + list(sink_flush() or [])
        return written

    # ------------------------------------------------------------- internals

    def _start(self, observation: TrackObservation) -> None:
        observed_at = _local_naive(observation.observed_at)
        position = observation.position_seconds
        duration = observation.duration_seconds
        started_at = observed_at
        if position is not None and 0 <= position <= self.max_played_seconds:
            started_at = observed_at - timedelta(seconds=position)
        title, artist, album = observation.identity
        self._current = {
            "identity": observation.identity,
            "title": title,
            "artist": artist,
            "album": album,
            "started_at": started_at,
            "first_position": position,
            "last_position": position,
            "duration_seconds": duration,
            "playing_seconds": 0.0,
            "first_observed_at": observed_at,
            "last_observed_at": observed_at,
            "playback_status": observation.playback_status,
        }

    def _step_seconds(self, observation: TrackObservation, gap: float) -> float:
        """Seconds of real playback between two observations."""
        if self._current is None:
            return 0.0
        if self._current["playback_status"] not in ("playing", "opened"):
            return 0.0
        step = min(max(gap, 0.0), self.max_step_seconds)
        first = self._current["last_position"]
        last = observation.position_seconds
        if first is not None and last is not None and last >= first:
            # The timeline is authoritative while it agrees with wall clock.
            advance = last - first
            if 0 <= advance <= max(step * 1.5, 5.0):
                return advance
        return step

    def _finalize(self) -> list[str]:
        current, self._current = self._current, None
        if current is None:
            return []
        played = self._played_seconds(current)
        if played <= 0:
            return []
        record = PlayRecord(
            title=current["title"] or current["artist"] or "未知曲目",
            artist=current["artist"] or None,
            album=current["album"] or None,
            started_at=current["started_at"],
            played_seconds=played,
            total_seconds=current["duration_seconds"],
            outcome=None,
        )
        return list(self.memory.record(record) or [])

    def _played_seconds(self, current: dict[str, Any]) -> float:
        """Prefer the position delta; fall back to accumulated playing time."""
        by_position = 0.0
        first, last = current["first_position"], current["last_position"]
        if first is not None and last is not None and last >= first:
            by_position = last - first
        played = max(current["playing_seconds"], by_position)
        return min(max(played, 0.0), self.max_played_seconds)
