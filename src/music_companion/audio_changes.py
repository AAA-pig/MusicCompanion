"""Incremental, local-only PCM16 music-change heuristics.

Events describe acoustic evidence, NOT chorus boundaries, emotion, song position,
or actual player pause actions. Times are relative to the capture/replay session.
`event_time_s` is when a condition first passed its threshold; `detected_at_s`
includes the confirmation window. Neither is a measured end-to-end audio latency.

Usage:
    detector = AudioChangeDetector(sample_rate=48000, channels=2)
    for event in detector.feed_pcm16(pcm_bytes):
        consume(event.to_dict())

Call reset() when capture packets are lost. Never replace missing packets with
zeroes: a capture failure must not be interpreted as musical silence.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
import math
from typing import Any

import numpy as np


@dataclass(frozen=True)
class DetectorConfig:
    hop_seconds: float = 0.05
    silence_dbfs: float = -60.0
    resume_dbfs: float = -52.0
    silence_hold_seconds: float = 0.7
    resume_hold_seconds: float = 0.25
    history_seconds: float = 8.0
    baseline_seconds: float = 4.0
    baseline_guard_seconds: float = 1.0
    minimum_baseline_seconds: float = 2.0
    recent_seconds: float = 0.4
    energy_rise_db: float = 6.0
    energy_drop_db: float = 8.0
    change_hold_seconds: float = 0.5
    texture_distance: float = 0.38
    texture_hold_seconds: float = 0.6
    rhythm_window_seconds: float = 1.5
    rhythm_ratio: float = 1.8
    rhythm_delta_per_second: float = 1.0
    rhythm_hold_seconds: float = 0.5
    event_cooldown_seconds: float = 8.0
    global_change_cooldown_seconds: float = 1.5

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        positive = [name for name in asdict(self) if name not in ("silence_dbfs", "resume_dbfs")]
        if any(getattr(self, name) <= 0 for name in positive):
            raise ValueError("Durations and detection thresholds must be positive")
        if not 0.01 <= self.hop_seconds <= 0.1:
            raise ValueError("hop_seconds must be between 0.01 and 0.1")
        if not self.silence_dbfs < self.resume_dbfs < 0:
            raise ValueError("Require silence_dbfs < resume_dbfs < 0")
        if self.history_seconds < self.baseline_seconds + self.baseline_guard_seconds:
            raise ValueError("History must contain the baseline and guard window")
        if self.minimum_baseline_seconds > self.baseline_seconds:
            raise ValueError("Minimum baseline cannot exceed the baseline window")
        if not 0 < self.texture_distance < 1:
            raise ValueError("texture_distance must be between 0 and 1")


@dataclass(frozen=True)
class ChangeEvent:
    type: str
    event_time_s: float
    detected_at_s: float
    stream_epoch: int
    evidence: dict[str, Any] = field(default_factory=dict)
    time_basis: str = "capture_session_seconds"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["event_time_s"] = round(self.event_time_s, 4)
        value["detected_at_s"] = round(self.detected_at_s, 4)
        value["confirmation_seconds"] = round(max(0.0, self.detected_at_s - self.event_time_s), 4)
        return value


@dataclass
class _Feature:
    end: float
    db: float
    bands: np.ndarray
    onset: bool


class AudioChangeDetector:
    """Stateful bounded-history detector; keep one instance per capture stream."""

    def __init__(self, sample_rate: int = 48000, channels: int = 2,
                 config: DetectorConfig | None = None, *, start_seconds: float = 0.0) -> None:
        if not 8000 <= sample_rate <= 192000 or not 1 <= channels <= 8:
            raise ValueError("Unsupported sample rate or channel count")
        if not math.isfinite(start_seconds) or start_seconds < 0:
            raise ValueError("start_seconds must be finite and nonnegative")
        self.sample_rate = sample_rate
        self.channels = channels
        self.config = config or DetectorConfig()
        self.hop_frames = round(sample_rate * self.config.hop_seconds)
        self.hop_seconds = self.hop_frames / sample_rate
        self._window = np.hanning(self.hop_frames).astype(np.float32)[:, None]
        self._fft_size = 1 << (self.hop_frames - 1).bit_length()
        frequencies = np.fft.rfftfreq(self._fft_size, 1 / sample_rate)
        edges = [0, 80, 200, 500, 1000, 2500, 6000, 12000, sample_rate / 2 + 1]
        edges = sorted(set(x for x in edges if x <= sample_rate / 2 + 1))
        self._band_masks = [(frequencies >= a) & (frequencies < b) for a, b in zip(edges, edges[1:])]
        self._history: deque[_Feature] = deque(maxlen=math.ceil(self.config.history_seconds / self.hop_seconds) + 2)
        self._pending: dict[str, float] = {}
        self._last_event: dict[str, float] = {}
        self._last_change = -math.inf
        self._origin = start_seconds
        self._frames = 0
        self._buffer = np.empty((0, channels), dtype=np.float32)
        self._previous_db: float | None = None
        self._previous_bands: np.ndarray | None = None
        self._last_onset = -math.inf
        self.stream_epoch = 0
        self.state = "initializing"
        self.last_features: dict[str, Any] = {}
        self.feature_frames = 0

    @property
    def time_seconds(self) -> float:
        return self._origin + self._frames / self.sample_rate

    def reset(self, at_seconds: float, reason: str = "capture_discontinuity") -> ChangeEvent:
        if not math.isfinite(at_seconds) or at_seconds < self.time_seconds - 1e-6:
            raise ValueError("Reset time must be finite and cannot move backwards")
        self.stream_epoch += 1
        self._origin = at_seconds
        self._frames = 0
        self._buffer = np.empty((0, self.channels), dtype=np.float32)
        self._history.clear()
        self._pending.clear()
        self._last_event.clear()
        self._last_change = -math.inf
        self._previous_db = None
        self._previous_bands = None
        self._last_onset = -math.inf
        self.state = "initializing"
        self.last_features = {}
        return ChangeEvent("capture_gap", at_seconds, at_seconds, self.stream_epoch, {"reason": reason})

    def feed_pcm16(self, pcm: bytes) -> list[ChangeEvent]:
        if len(pcm) % (2 * self.channels):
            raise ValueError("PCM chunk must contain complete interleaved frames")
        if not pcm:
            return []
        samples = np.frombuffer(pcm, dtype="<i2").reshape(-1, self.channels).astype(np.float32) / 32768.0
        return self.feed(samples)

    def feed(self, samples: np.ndarray) -> list[ChangeEvent]:
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim == 1 and self.channels == 1:
            samples = samples[:, None]
        if samples.ndim != 2 or samples.shape[1] != self.channels:
            raise ValueError("Expected an array with shape (frames, channels)")
        if not np.isfinite(samples).all() or (samples.size and np.max(np.abs(samples)) > 1.0001):
            raise ValueError("Samples must be finite normalized values in [-1, 1]")
        self._buffer = np.concatenate((self._buffer, samples), axis=0)
        events: list[ChangeEvent] = []
        consumed = 0
        while consumed + self.hop_frames <= len(self._buffer):
            block = self._buffer[consumed:consumed + self.hop_frames]
            self._frames += self.hop_frames
            feature = self._extract(block, self.time_seconds)
            events.extend(self._advance(feature))
            self.feature_frames += 1
            consumed += self.hop_frames
        self._buffer = self._buffer[consumed:].copy()
        return events

    def _extract(self, block: np.ndarray, end: float) -> _Feature:
        # Average CHANNEL POWERS, not channels themselves; antiphase stereo must
        # not disappear through a naive mono downmix.
        rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))
        db = max(-120.0, 20 * math.log10(max(rms, 1e-6)))
        fft = np.fft.rfft(block * self._window, n=self._fft_size, axis=0)
        power = np.mean(np.abs(fft) ** 2, axis=1)
        bands = np.array([float(power[mask].sum()) for mask in self._band_masks])
        bands /= max(float(bands.sum()), 1e-20)
        db_jump = db - self._previous_db if self._previous_db is not None else 0.0
        flux = float(np.maximum(bands - self._previous_bands, 0).sum()) if self._previous_bands is not None else 0.0
        onset = (db > self.config.resume_dbfs and end - self._last_onset >= 0.12
                 and (db_jump >= 4.0 or (flux >= 0.2 and db_jump >= 1.5)))
        if onset:
            self._last_onset = end
        self._previous_db, self._previous_bands = db, bands
        return _Feature(end, db, bands, onset)

    def _confirmed(self, name: str, condition: bool, end: float, hold: float) -> float | None:
        if not condition:
            self._pending.pop(name, None)
            return None
        since = self._pending.setdefault(name, end - self.hop_seconds)
        return since if end - since >= hold - 1e-8 else None

    def _emit(self, name: str, since: float, end: float, evidence: dict[str, Any]) -> ChangeEvent:
        self._pending.pop(name, None)
        self._last_event[name] = end
        return ChangeEvent(name, max(0.0, since), end, self.stream_epoch, evidence)

    def _advance(self, current: _Feature) -> list[ChangeEvent]:
        c, now = self.config, current.end
        self.last_features = {"rms_dbfs": round(current.db, 2), "onset": current.onset}
        quiet_since = self._confirmed("quiet", current.db < c.silence_dbfs, now, c.silence_hold_seconds)
        audible_since = self._confirmed("audible", current.db > c.resume_dbfs, now, c.resume_hold_seconds)
        if self.state != "silence" and quiet_since is not None:
            self.state = "silence"
            self._history.clear()
            self._pending.clear()
            return [self._emit("silence_start", quiet_since, now, {"rms_dbfs": round(current.db, 2), "interpretation": "silence_or_pause_candidate"})]
        if self.state != "audio" and audible_since is not None:
            previous = self.state
            self.state = "audio"
            self._history.clear()
            self._pending.clear()
            self._history.append(current)
            return [self._emit("audio_resumed" if previous == "silence" else "audio_started", audible_since, now,
                               {"rms_dbfs": round(current.db, 2)})]
        if self.state != "audio" or current.db < c.silence_dbfs:
            for key in ("energy_rise", "energy_drop", "texture_change", "rhythm_change"):
                self._pending.pop(key, None)
            return []
        self._history.append(current)
        history = list(self._history)
        baseline = [f for f in history if now - c.baseline_guard_seconds - c.baseline_seconds < f.end <= now - c.baseline_guard_seconds + 1e-8]
        recent = [f for f in history if f.end > now - c.recent_seconds + 1e-8]
        if len(baseline) * self.hop_seconds < c.minimum_baseline_seconds - 1e-8 or not recent:
            return []
        base_db = float(np.median([f.db for f in baseline]))
        recent_db = float(np.median([f.db for f in recent]))
        delta_db = recent_db - base_db
        base_bands = np.median(np.stack([f.bands for f in baseline]), axis=0)
        new_bands = np.median(np.stack([f.bands for f in recent]), axis=0)
        base_bands /= max(float(base_bands.sum()), 1e-20)
        new_bands /= max(float(new_bands.sum()), 1e-20)
        distance = float(np.abs(new_bands - base_bands).sum() / 2)
        rhythm_recent = [f for f in history if f.end > now - c.rhythm_window_seconds + 1e-8]
        rhythm_base = [f for f in history if now - c.rhythm_window_seconds - c.baseline_seconds < f.end <= now - c.rhythm_window_seconds + 1e-8]
        rhythm_ready = len(rhythm_base) * self.hop_seconds >= c.minimum_baseline_seconds - 1e-8
        onset_rate = sum(f.onset for f in rhythm_recent) / max(self.hop_seconds, len(rhythm_recent) * self.hop_seconds)
        base_rate = sum(f.onset for f in rhythm_base) / max(self.hop_seconds, len(rhythm_base) * self.hop_seconds)
        ratio = (onset_rate + 0.25) / (base_rate + 0.25)
        rhythm_change = rhythm_ready and abs(onset_rate - base_rate) >= c.rhythm_delta_per_second and (ratio >= c.rhythm_ratio or ratio <= 1 / c.rhythm_ratio)
        evidence = {
            "rms_dbfs": round(recent_db, 2), "baseline_rms_dbfs": round(base_db, 2),
            "delta_db": round(delta_db, 2), "spectral_distance": round(distance, 4),
            "onsets_per_second": round(onset_rate, 3), "baseline_onsets_per_second": round(base_rate, 3),
        }
        self.last_features.update(evidence)
        candidates = [
            ("energy_rise", delta_db >= c.energy_rise_db, c.change_hold_seconds),
            ("energy_drop", delta_db <= -c.energy_drop_db, c.change_hold_seconds),
            ("texture_change", distance >= c.texture_distance, c.texture_hold_seconds),
            ("rhythm_change", rhythm_change, c.rhythm_hold_seconds),
        ]
        ready = []
        for name, condition, hold in candidates:
            since = self._confirmed(name, condition, now, hold)
            if since is not None and now - self._last_event.get(name, -math.inf) >= c.event_cooldown_seconds:
                ready.append((name, since))
        if ready and now - self._last_change >= c.global_change_cooldown_seconds:
            name, since = ready[0]
            self._last_change = now
            return [self._emit(name, since, now, evidence)]
        return []

