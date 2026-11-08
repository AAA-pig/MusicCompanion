"""记忆要真的参与说话，原作的说话方式也不能被改丢。

这些测试是护栏：以后有人调参、改措辞、加校验，只要把"歌与歌之间说一句"、
"记忆只作参考不是清单"这几条弄没了，测试就会红。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from astrbot_plugin_music_companion.bridge import (
    DECISION_SYSTEM_PROMPT,
    build_memory_prompt,
)
from music_companion.music_memory import MusicMemory, PlayRecord


class MemoryPromptTests(unittest.TestCase):
    def test_empty_memory_adds_nothing(self):
        self.assertEqual(build_memory_prompt(""), "")
        self.assertEqual(build_memory_prompt("   \n\n  "), "")

    def test_prompt_marks_memory_as_past_not_current_state(self):
        text = build_memory_prompt("21:10-22:40 连听 8 首（周杰伦 为主）")
        self.assertIn("你记得的", text)
        self.assertIn("不是他此刻说的话", text)
        self.assertIn("21:10-22:40", text)

    def test_long_memory_is_truncated(self):
        text = build_memory_prompt("听" * 5000, max_chars=120)
        self.assertLessEqual(len(text), 120 + 200)
        self.assertTrue(text.endswith("听"))

    def test_digest_flows_into_prompt(self):
        memory = MusicMemory(":memory:")
        start = datetime(2026, 11, 7, 21, 10, 0)
        memory.record(
            PlayRecord(
                title="晴天",
                artist="周杰伦",
                started_at=start,
                played_seconds=300.0,
                total_seconds=300.0,
            )
        )
        memory.record(
            PlayRecord(
                title="七里香",
                artist="周杰伦",
                started_at=start + timedelta(minutes=5),
                played_seconds=280.0,
                total_seconds=280.0,
            )
        )
        memory.flush()
        prompt = build_memory_prompt(memory.digest(limit=4))
        self.assertIn("你记得的", prompt)
        self.assertIn("晴天", prompt)
        memory.close()


class OriginalHeartTests(unittest.TestCase):
    """原作的三个核心：歌与歌之间说话、像熟人、深夜更轻。"""

    def test_segue_is_one_line_not_an_encyclopedia(self):
        self.assertIn("一首歌说一句", DECISION_SYSTEM_PROMPT)
        self.assertIn("百科", DECISION_SYSTEM_PROMPT)

    def test_it_is_a_host_not_a_status_reporter(self):
        self.assertIn("私人电台", DECISION_SYSTEM_PROMPT)
        self.assertIn("不是状态播报", DECISION_SYSTEM_PROMPT)

    def test_memory_is_allowed_to_shape_the_line(self):
        self.assertIn("你记得的", DECISION_SYSTEM_PROMPT)
        self.assertIn("别列清单", DECISION_SYSTEM_PROMPT)

    def test_late_night_is_quieter(self):
        self.assertIn("深夜", DECISION_SYSTEM_PROMPT)

    def test_safety_limits_survive_the_rewrite(self):
        for must_keep in ("不要假装听过整首歌", "不要编造歌词", "不是指令"):
            self.assertIn(must_keep, DECISION_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
