"""Tests for the behavioural memory layer (step 1 of the agreed plan)."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from music_companion.music_memory import (
    LISTENED,
    SKIPPED,
    UNKNOWN,
    MusicMemory,
    PlayRecord,
)

T0 = datetime(2026, 11, 7, 23, 10, 0)


def rec(title: str, minute: float, *, artist: str = "某歌手", played: float = 240.0,
        total: float | None = 240.0, outcome: str | None = None) -> PlayRecord:
    return PlayRecord(
        title=title,
        artist=artist,
        album="某专辑",
        started_at=T0 + timedelta(minutes=minute),
        played_seconds=played,
        total_seconds=total,
        outcome=outcome,
    )


class OutcomeTests(unittest.TestCase):
    def test_full_play_is_listened(self):
        r = rec("A", 0, played=235.0)
        self.assertEqual(
            r.resolved_outcome(skip_max_seconds=20, listen_ratio=0.9), LISTENED
        )

    def test_quick_switch_is_skipped(self):
        r = rec("A", 0, played=12.0)
        self.assertEqual(
            r.resolved_outcome(skip_max_seconds=20, listen_ratio=0.9), SKIPPED
        )

    def test_partial_without_duration_is_unknown(self):
        r = rec("A", 0, played=90.0, total=None)
        self.assertEqual(
            r.resolved_outcome(skip_max_seconds=20, listen_ratio=0.9), UNKNOWN
        )

    def test_explicit_outcome_wins(self):
        r = rec("A", 0, played=240.0, outcome=SKIPPED)
        self.assertEqual(
            r.resolved_outcome(skip_max_seconds=20, listen_ratio=0.9), SKIPPED
        )


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "music.db"
        self.memory = MusicMemory(self.db)
        self.addCleanup(self.memory.close)

    def test_counters_are_updated_per_track(self):
        self.assertEqual(self.memory.record(rec("A", 0)), [])
        self.assertEqual(self.memory.record(rec("A", 4)), [])
        self.memory.record(rec("B", 12, played=8.0, outcome=SKIPPED))
        tracks = {row["title"]: row for row in self.memory.favourite_tracks()}
        self.assertEqual(tracks["A"]["listened"], 2)
        self.assertEqual(tracks["A"]["skipped"], 0)
        self.assertNotIn("B", tracks)  # skipped-only tracks never show as favourites

    def test_gap_longer_than_five_minutes_flushes_one_summary(self):
        self.memory.record(rec("A", 0))
        self.memory.record(rec("B", 4))
        written = self.memory.record(rec("C", 14))  # 6 分钟空白，前一段收尾
        self.assertEqual(len(written), 1)
        self.assertIn("连听 2 首", written[0])
        self.assertIn("23:10-23:18", written[0])

    def test_continuous_play_stays_in_one_session(self):
        self.memory.record(rec("A", 0))
        self.memory.record(rec("B", 4))
        self.memory.record(rec("C", 8))
        self.assertEqual(self.memory.recent_notes(), [])
        written = self.memory.flush()
        self.assertEqual(len(written), 1)
        self.assertIn("连听 3 首", written[0])

    def test_skips_are_counted_in_the_summary(self):
        self.memory.record(rec("A", 0))
        self.memory.record(rec("B", 4.2, played=9.0, outcome=SKIPPED))
        self.memory.record(rec("C", 4.4, played=11.0, outcome=SKIPPED))
        written = self.memory.flush()
        self.assertIn("跳过 2 首", written[0])

    def test_artist_appears_only_when_repeated(self):
        self.memory.record(rec("A", 0, artist="周杰伦"))
        self.memory.record(rec("B", 4, artist="周杰伦"))
        self.assertIn("周杰伦", self.memory.flush()[0])

    def test_repeat_full_listen_becomes_its_own_note(self):
        self.memory.record(rec("晴天", 0))
        self.memory.record(rec("别的歌", 4))
        self.memory.record(rec("晴天", 8))
        written = self.memory.flush()
        self.assertEqual(len(written), 2)
        self.assertIn("《晴天》这一段里完整听了 2 次", written[1])
        notes = [n["text"] for n in self.memory.recent_notes()]
        self.assertEqual(len(notes), 2)

    def test_recent_notes_are_newest_first(self):
        self.memory.record(rec("A", 0))
        self.memory.record(rec("B", 20))  # 触发第一段收尾
        self.memory.record(rec("C", 40))
        self.memory.flush()
        notes = [n["text"] for n in self.memory.recent_notes()]
        self.assertTrue(notes[0].startswith("23:50"))
        self.assertTrue(notes[-1].startswith("23:10"))

    def test_skipped_tracks_need_two_skips_and_more_skips_than_listens(self):
        for minute in (0, 6):
            self.memory.record(rec("烦人歌", minute, played=5.0, outcome=SKIPPED))
        self.memory.record(rec("一般歌", 12, played=5.0, outcome=SKIPPED))
        titles = [row["title"] for row in self.memory.skipped_tracks(min_skips=2)]
        self.assertEqual(titles, ["烦人歌"])

    def test_unknown_outcome_does_not_touch_preferences(self):
        self.memory.record(rec("A", 0, played=60.0, total=None))
        self.assertEqual(self.memory.favourite_tracks(), [])
        self.assertEqual(self.memory.skipped_tracks(min_skips=1), [])

    def test_empty_digest_is_honest(self):
        self.assertEqual(self.memory.digest(), "音乐记忆还是空的。")

    def test_digest_contains_notes_and_favourites(self):
        self.memory.record(rec("晴天", 0))
        self.memory.record(rec("七里香", 4))
        self.memory.flush()
        digest = self.memory.digest()
        self.assertIn("最近的听歌记录", digest)
        self.assertIn("晴天", digest)

    def test_diary_is_built_and_updated_in_place(self):
        self.memory.record(rec("晴天", 0))
        self.memory.record(rec("七里香", 4))
        self.memory.flush()
        text = self.memory.build_diary(datetime(2026, 11, 7, 23, 30))
        self.assertIn("11 月 7 日", text)
        self.assertIn("听了 2 首", text)
        self.memory.build_diary(datetime(2026, 11, 7, 23, 59))
        rows = self.memory._conn.execute("SELECT COUNT(*) FROM diary").fetchone()[0]
        self.assertEqual(rows, 1)
        self.assertEqual(self.memory.diary(datetime(2026, 11, 7, 12, 0)), text)

    def test_diary_for_a_quiet_day(self):
        text = self.memory.build_diary(datetime(2026, 11, 8, 9, 0))
        self.assertIn("没有留下听歌记录", text)

    def test_reopening_the_same_file_keeps_history(self):
        self.memory.record(rec("晴天", 0))
        self.memory.flush()
        self.memory.close()
        again = MusicMemory(self.db)
        self.addCleanup(again.close)
        self.assertEqual(again.favourite_tracks()[0]["title"], "晴天")
        self.assertEqual(len(again.recent_notes()), 1)


class GuardTests(unittest.TestCase):
    def test_invalid_configuration_is_rejected(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "music.db"
        with self.assertRaises(ValueError):
            MusicMemory(path, session_gap_seconds=0)
        with self.assertRaises(ValueError):
            MusicMemory(path, listen_ratio=1.5)


if __name__ == "__main__":
    unittest.main()
