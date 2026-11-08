"""Tests for the read-side formatting used by /音乐记忆 and the recall tool."""
from __future__ import annotations

import unittest

from astrbot_plugin_music_companion.bridge import (
    format_note_lines,
    format_track_lines,
)


class NoteFormatTests(unittest.TestCase):
    def test_note_gets_a_short_stamp(self):
        lines = format_note_lines(
            [{"started_at": "2026-11-07T21:40:12", "text": "听了 6 首"}]
        )
        self.assertEqual(lines, ["- 11-7 21:40 听了 6 首"])

    def test_bad_or_missing_stamp_still_prints_the_text(self):
        lines = format_note_lines([{"started_at": "", "text": "听了一小段"}])
        self.assertEqual(lines, ["- 听了一小段"])

    def test_limit_and_blank_rows_are_respected(self):
        rows = [
            {"started_at": "2026-11-07T21:40:12", "text": "a"},
            {"started_at": "2026-11-07T21:50:12", "text": "   "},
            {"started_at": "2026-11-07T22:00:12", "text": "b"},
        ]
        self.assertEqual(format_note_lines(rows, limit=1), ["- 11-7 21:40 a"])
        self.assertEqual(format_note_lines(rows, limit=5), ["- 11-7 21:40 a", "- 11-7 22:00 b"])

    def test_empty_input_returns_no_lines(self):
        self.assertEqual(format_note_lines([]), [])
        self.assertEqual(format_note_lines(None), [])


class TrackFormatTests(unittest.TestCase):
    def test_listened_track_shows_count_and_minutes(self):
        lines = format_track_lines(
            [{"title": "晴天", "artist": "周杰伦", "listened": 3, "played_seconds": 600}]
        )
        self.assertEqual(lines, ["- 《晴天》（周杰伦） 完整听了 3 次，累计 10 分钟"])

    def test_short_play_time_omits_the_minute_tail(self):
        lines = format_track_lines(
            [{"title": "晴天", "artist": "", "listened": 1, "played_seconds": 20}]
        )
        self.assertEqual(lines, ["- 《晴天》 完整听了 1 次"])

    def test_skipped_mode_reports_skips(self):
        lines = format_track_lines(
            [{"title": "某首歌", "artist": "谁", "skipped": 4, "listened": 0}],
            mode="skipped",
        )
        self.assertEqual(lines, ["- 《某首歌》（谁） 被跳过 4 次"])

    def test_rows_without_a_title_are_dropped(self):
        self.assertEqual(format_track_lines([{"artist": "谁"}]), [])
        self.assertEqual(format_track_lines([], limit=5), [])


if __name__ == "__main__":
    unittest.main()
