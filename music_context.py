"""Read the current Windows media session through GSMTC (read-only).

This module does not capture audio, control playback, access account credentials,
or infer song position when Windows does not provide a timeline.

Optional dependency: the PyWinRT Windows.Media.Control projection. Import is lazy
so the rest of the project and its tests remain usable without Windows/PyWinRT.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator, Callable, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MusicContext:
    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    playback_status: str = "unknown"
    position_seconds: Optional[float] = None
    position_source: str = "unavailable"
    position_confidence: str = "none"
    duration_seconds: Optional[float] = None
    source_app_id: Optional[str] = None
    sampled_at_monotonic: float = 0.0
    available: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class GSMTCUnavailableError(RuntimeError):
    """Raised when the Windows GSMTC API cannot be imported or initialized."""


def _enum_name(value: Any) -> str:
    if value is None:
        return "unknown"
    name = getattr(value, "name", None)
    if name:
        return str(name).lower()
    text = str(value).rsplit(".", 1)[-1]
    return text.lower() if text else "unknown"


def _seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    total = getattr(value, "total_seconds", None)
    if callable(total):
        try:
            result = float(total())
            return result if result >= 0 else None
        except (TypeError, ValueError, OverflowError):
            return None
    # Some projections expose TimeSpan as an integer count of 100 ns ticks.
    try:
        result = float(value) / 10_000_000.0
        return result if result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


class MusicContextReader:
    """Poll the current GSMTC session without touching the audio stream.

    `session_manager` is injectable for tests. Production initialization uses
    `await MusicContextReader.create()`. By default, sessions whose source app
    id does not contain `cloudmusic` are rejected to avoid reading another app.
    Set `source_app_tokens=()` only if the local Windows installation exposes
    a different, verified app id.
    """

    def __init__(
        self,
        session_manager: Any,
        *,
        source_app_tokens: tuple[str, ...] = ("cloudmusic",),
    ) -> None:
        self._manager = session_manager
        self._source_app_tokens = tuple(t.casefold() for t in source_app_tokens)

    @classmethod
    async def create(
        cls,
        *,
        source_app_tokens: tuple[str, ...] = ("cloudmusic",),
    ) -> "MusicContextReader":
        try:
            from winrt.windows.media.control import (  # type: ignore[import-not-found]
                GlobalSystemMediaTransportControlsSessionManager,
            )
        except Exception as exc:  # Import errors vary by PyWinRT distribution.
            raise GSMTCUnavailableError(
                "PyWinRT Windows.Media.Control is unavailable. Install a compatible "
                "PyWinRT projection on Windows, then retry."
            ) from exc
        try:
            manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        except Exception as exc:
            raise GSMTCUnavailableError(f"GSMTC initialization failed: {exc}") from exc
        return cls(manager, source_app_tokens=source_app_tokens)

    def _is_target_app(self, app_id: Optional[str]) -> bool:
        if not self._source_app_tokens:
            return True
        if not app_id:
            return False
        folded = app_id.casefold()
        return any(token in folded for token in self._source_app_tokens)

    async def read(self) -> MusicContext:
        """Read one snapshot. API failures are returned as an unavailable context."""
        sampled = time.monotonic()
        try:
            session = self._manager.get_current_session()
            if session is None:
                return MusicContext(sampled_at_monotonic=sampled)

            app_id = getattr(session, "source_app_user_model_id", None)
            if not self._is_target_app(app_id):
                return MusicContext(
                    source_app_id=str(app_id) if app_id else None,
                    sampled_at_monotonic=sampled,
                )

            playback = session.get_playback_info()
            status_obj = getattr(playback, "playback_status", None)
            status = _enum_name(status_obj)

            media = await session.try_get_media_properties_async()
            timeline = session.get_timeline_properties()
            position = _seconds(getattr(timeline, "position", None))
            duration = _seconds(getattr(timeline, "end_time", None))
            # This NetEase build exposes 0/0 when no usable timeline is available.
            if position == 0.0 and duration == 0.0:
                position = None
                duration = None

            return MusicContext(
                title=_clean(getattr(media, "title", None)),
                artist=_clean(getattr(media, "artist", None)),
                album=_clean(getattr(media, "album_title", None)),
                playback_status=status,
                position_seconds=position,
                position_source="gsmTC_timeline" if position is not None else "unavailable",
                position_confidence="system_reported" if position is not None else "none",
                duration_seconds=duration,
                source_app_id=str(app_id) if app_id else None,
                sampled_at_monotonic=sampled,
                available=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("GSMTC read failed: %s", exc)
            return MusicContext(sampled_at_monotonic=sampled, error=f"{type(exc).__name__}: {exc}")

    async def watch(
        self,
        *,
        interval_seconds: float = 1.0,
        include_position_updates: bool = False,
    ) -> AsyncIterator[MusicContext]:
        """Yield snapshots on track/status changes; optionally yield position ticks."""
        if interval_seconds < 0.2:
            raise ValueError("interval_seconds must be >= 0.2")
        previous_key: Any = object()
        while True:
            context = await self.read()
            key = (
                context.title,
                context.artist,
                context.playback_status,
                context.source_app_id,
                context.available,
                context.error,
            )
            if include_position_updates:
                key += (None if context.position_seconds is None else round(context.position_seconds, 1),)
            if key != previous_key:
                yield context
                previous_key = key
            await asyncio.sleep(interval_seconds)


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


async def read_current_music_context() -> MusicContext:
    """Convenience one-shot read using the real Windows GSMTC API."""
    reader = await MusicContextReader.create()
    return await reader.read()


class MusicContextPoller:
    """Poll GSMTC independently and queue deduplicated context events."""

    def __init__(
        self,
        reader: Any = None,
        *,
        interval_seconds: float = 1.0,
        unavailable_after_seconds: float = 3.0,
    ) -> None:
        if interval_seconds < 0.2:
            raise ValueError("interval_seconds must be >= 0.2")
        if unavailable_after_seconds < 0:
            raise ValueError("unavailable_after_seconds must be nonnegative")
        self._reader = reader
        self.interval_seconds = interval_seconds
        self.unavailable_after_seconds = unavailable_after_seconds
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._latest: MusicContext | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._confirmed_track: tuple[Any, ...] | None = None
        self._pending_track: tuple[Any, ...] | None = None
        self._playback_status: str | None = None
        self._last_valid_at: float | None = None
        self._unavailable_since: float | None = None
        self._unavailable_reported = False

    @property
    def latest(self) -> MusicContext | None:
        with self._lock:
            return self._latest

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("MusicContextPoller can only be started once")
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name="music-context-poller"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=self.interval_seconds + 2)

    def drain_events(self) -> list[dict[str, Any]]:
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            context = MusicContext(
                sampled_at_monotonic=time.monotonic(),
                error=f"{type(exc).__name__}: {exc}",
            )
            with self._lock:
                self._latest = context
            self._emit("music_context_error", context)

    async def _run(self) -> None:
        reader = self._reader or await MusicContextReader.create()
        while not self._stop.is_set():
            try:
                context = await reader.read()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                context = MusicContext(
                    sampled_at_monotonic=time.monotonic(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            self.process_snapshot(context)
            await asyncio.sleep(self.interval_seconds)

    def process_snapshot(self, context: MusicContext) -> None:
        now = context.sampled_at_monotonic or time.monotonic()
        if context.available:
            with self._lock:
                self._latest = context
            if self._unavailable_reported:
                self._emit("music_context_recovered", context)
            self._unavailable_reported = False
            self._unavailable_since = None
            self._last_valid_at = now
            self._process_available(context)
            return

        with self._lock:
            if self._latest is None:
                self._latest = context
        if self._unavailable_since is None:
            self._unavailable_since = now
        unavailable_reference = (
            self._last_valid_at
            if self._last_valid_at is not None
            else self._unavailable_since
        )
        if now - unavailable_reference < self.unavailable_after_seconds:
            return
        with self._lock:
            self._latest = context
        if not self._unavailable_reported:
            self._unavailable_reported = True
            self._emit("music_context_unavailable", context)

    def _process_available(self, context: MusicContext) -> None:
        identity = self._track_identity(context)
        if self._confirmed_track is None:
            self._confirmed_track = identity
            self._emit("track_observed", context)
        elif identity != self._confirmed_track:
            if self._pending_track != identity:
                self._pending_track = identity
            else:
                previous = self._confirmed_track
                self._confirmed_track = identity
                self._pending_track = None
                self._emit(
                    "track_changed",
                    context,
                    previous_track=self._track_details(previous),
                )
        else:
            self._pending_track = None

        if (
            self._playback_status is not None
            and context.playback_status != self._playback_status
        ):
            self._emit(
                "playback_changed",
                context,
                previous_status=self._playback_status,
            )
        self._playback_status = context.playback_status

    def _emit(
        self,
        event_type: str,
        context: MusicContext,
        **extra: Any,
    ) -> None:
        self._events.put({
            "kind": "music_context_event",
            "type": event_type,
            "observed_at_monotonic": time.monotonic(),
            "music_context": context.to_dict(),
            **extra,
        })

    @staticmethod
    def _track_identity(context: MusicContext) -> tuple[Any, ...]:
        return (
            context.title,
            context.artist,
            context.album,
            context.source_app_id,
        )

    @staticmethod
    def _track_details(identity: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "title": identity[0],
            "artist": identity[1],
            "album": identity[2],
            "source_app_id": identity[3],
        }
