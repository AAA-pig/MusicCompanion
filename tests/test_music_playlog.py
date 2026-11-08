"""Tests for the observation -> PlayRecord bridge (step 2 of the plan)."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from music_companion.music_memory import MusicMemory
from music_companion.music_playlog import PlayLogRecorder, TrackObservation

T0 = datetime(2026, 11, 7, 23, 10, 0)


def obs(title, at, *, position=None, duration=240.0, artist="某歌手",
        status="playing") -> TrackObservation:
    return TrackObservation(
        observed_at=T0 + timedelta(seconds=at),
        title=title,
        artist=artist,
        album="某专辑",
        position_seconds=position,
        duration_seconds=duration,
        playback_status=status,
    )


class FakeSink:
    def __init__(self):
        self.records = []

    def record(self, record):
        self.records.append(record)
        return []


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.sink = FakeSink()
        # 这批用例按「3 分钟没动静就算人走了」的假设写，便于构造场景。
        self.recorder = PlayLogRecorder(self.sink, stale_after_seconds=180)

    def test_first_observation_only_arms_the_tracker(self):
        self.assertEqual(self.recorder.observe(obs("A", 0, position=0)), [])
        self.assertEqual(self.sink.records, [])
        self.assertEqual(self.recorder.current_title, "A")

    def test_single_observation_track_is_recorded_when_position_is_missing(self):
        # Windows 常常不给播放进度：一首歌只会被看到一次，换歌那一刻就是它的收尾。
        self.recorder.observe(obs("A", 0, position=None))
        self.recorder.observe(obs("B", 245, position=None))
        self.assertEqual([record.title for record in self.sink.records], ["A"])
        self.assertAlmostEqual(self.sink.records[0].played_seconds, 245.0, places=3)

    def test_track_change_emits_one_finished_record(self):
        self.recorder.observe(obs("A", 0, position=0))
        for second in (60, 120, 180, 235):
            self.recorder.observe(obs("A", second, position=second))
        self.recorder.observe(obs("B", 236, position=0))
        self.assertEqual(len(self.sink.records), 1)
        record = self.sink.records[0]
        self.assertEqual(record.title, "A")
        self.assertEqual(record.total_seconds, 240.0)
        self.assertAlmostEqual(record.played_seconds, 235.0, delta=1.0)
        self.assertEqual(record.resolved_outcome(skip_max_seconds=20, listen_ratio=0.9), "listened")

    def test_quick_switch_becomes_a_skip(self):
        self.recorder.observe(obs("A", 0, position=0))
        self.recorder.observe(obs("A", 8, position=8))
        self.recorder.observe(obs("B", 9, position=0))
        record = self.sink.records[0]
        self.assertLess(record.played_seconds, 20)
        self.assertEqual(record.resolved_outcome(skip_max_seconds=20, listen_ratio=0.9), "skipped")

    def test_paused_stretch_does_not_count_as_listening(self):
        self.recorder.observe(obs("A", 0, position=0))
        self.recorder.observe(obs("A", 30, position=30, status="paused"))
        self.recorder.observe(obs("A", 90, position=30, status="paused"))
        self.recorder.observe(obs("B", 91, position=0))
        record = self.sink.records[0]
        self.assertLess(record.played_seconds, 40)

    def test_long_gap_finalizes_without_a_new_track(self):
        self.recorder.observe(obs("A", 0, position=0))
        self.recorder.observe(obs("A", 60, position=60))
        self.recorder.observe(obs("A", 600, position=None))
        self.assertEqual(len(self.sink.records), 1)
        self.assertEqual(self.sink.records[0].title, "A")

    def test_seek_backwards_falls_back_to_wall_clock(self):
        self.recorder.observe(obs("A", 0, position=200))
        self.recorder.observe(obs("A", 30, position=10))
        self.recorder.observe(obs("B", 31, position=0))
        record = self.sink.records[0]
        self.assertAlmostEqual(record.played_seconds, 30.0, delta=2.0)

    def test_missing_timeline_still_counts_wall_clock(self):
        recorder = PlayLogRecorder(FakeSink())
        recorder.observe(obs("A", 0, position=None, duration=None))
        recorder.observe(obs("A", 45, position=None, duration=None))
        recorder.observe(obs("B", 46, position=None, duration=None))
        record = recorder.memory.records[0]
        self.assertAlmostEqual(record.played_seconds, 45.0, delta=2.0)
        self.assertEqual(
            record.resolved_outcome(skip_max_seconds=20, listen_ratio=0.9), "unknown"
        )

    def test_empty_observations_are_ignored(self):
        self.assertEqual(self.recorder.observe(TrackObservation(observed_at=T0)), [])
        self.assertIsNone(self.recorder.current_title)

    def test_flush_emits_the_track_in_flight(self):
        self.recorder.observe(obs("A", 0, position=0))
        self.recorder.observe(obs("A", 100, position=100))
        self.recorder.flush()
        self.assertEqual(len(self.sink.records), 1)
        self.recorder.flush()
        self.assertEqual(len(self.sink.records), 1)

    def test_artist_only_track_is_kept(self):
        self.recorder.observe(TrackObservation(observed_at=T0, artist="纯音乐", position_seconds=0))
        self.recorder.observe(TrackObservation(observed_at=T0 + timedelta(seconds=30), artist="纯音乐", position_seconds=30))
        self.recorder.flush()
        self.assertEqual(self.sink.records[0].artist, "纯音乐")


class MemoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.memory = MusicMemory(Path(self._tmp.name) / "music.db")
        self.addCleanup(self.memory.close)
        self.recorder = PlayLogRecorder(self.memory)

    def test_a_session_reaches_the_memory_layer(self):
        for second in (0, 60, 120, 180, 240):
            self.recorder.observe(obs("晴天", second, position=second))
        for second in (241, 301, 361, 421, 481):
            self.recorder.observe(obs("七里香", second, position=second - 241))
        self.recorder.flush()
        favourites = self.memory.favourite_tracks()
        self.assertEqual([row["title"] for row in favourites], ["晴天", "七里香"])
        notes = [row["text"] for row in self.memory.recent_notes()]
        self.assertEqual(len(notes), 1)
        self.assertIn("连听 2 首", notes[0])

    def test_tracks_far_apart_become_two_sessions(self):
        for second in (0, 60, 120, 180, 240):
            self.recorder.observe(obs("晴天", second, position=second))
        for second in (1200, 1260, 1320, 1380, 1440):
            self.recorder.observe(obs("七里香", second, position=second - 1200))
        self.recorder.flush()
        notes = [row["text"] for row in self.memory.recent_notes()]
        self.assertEqual(len(notes), 2)


if __name__ == "__main__":
    unittest.main()
