"""Tests for the record -> observation mapping used by the plugin wiring."""
from __future__ import annotations

import unittest

from astrbot_plugin_music_companion.bridge import observation_from_record


def context(**overrides):
    base = {
        "title": "晴天",
        "artist": "周杰伦",
        "album": "叶惠美",
        "playback_status": "playing",
        "position_seconds": 61.5,
        "duration_seconds": 269.0,
        "available": True,
    }
    base.update(overrides)
    return base


def record(kind="music_context_event", type_="track_changed", **overrides):
    payload = {
        "kind": kind,
        "type": type_,
        "logged_at_utc": "2026-11-07T15:10:00+00:00",
        "music_context": context(**overrides),
    }
    return payload


class ObservationMappingTests(unittest.TestCase):
    def test_track_change_maps_to_the_expected_fields(self):
        mapped = observation_from_record(record())
        self.assertEqual(mapped["title"], "晴天")
        self.assertEqual(mapped["artist"], "周杰伦")
        self.assertEqual(mapped["position_seconds"], 61.5)
        self.assertEqual(mapped["duration_seconds"], 269.0)
        self.assertEqual(mapped["playback_status"], "playing")
        self.assertEqual(mapped["logged_at_utc"], "2026-11-07T15:10:00+00:00")

    def test_capture_gap_never_reaches_memory(self):
        gap = {"kind": "event", "type": "capture_gap", "detected_at_s": 12.0}
        self.assertIsNone(observation_from_record(gap))

    def test_non_memory_event_types_are_ignored(self):
        self.assertIsNone(observation_from_record(record(type_="energy_rise")))

    def test_unavailable_snapshot_is_ignored(self):
        self.assertIsNone(observation_from_record(record(available=False, title=None)))

    def test_missing_context_is_ignored(self):
        self.assertIsNone(observation_from_record({"kind": "music_context_event", "type": "track_changed"}))

    def test_nan_position_becomes_none(self):
        mapped = observation_from_record(record(position_seconds=float("nan")))
        self.assertIsNone(mapped["position_seconds"])

    def test_missing_status_falls_back_to_unknown(self):
        mapped = observation_from_record(record(playback_status=None))
        self.assertEqual(mapped["playback_status"], "unknown")


if __name__ == "__main__":
    unittest.main()
