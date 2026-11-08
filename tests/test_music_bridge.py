from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

from astrbot_plugin_music_companion.bridge import (
    JsonlTailer,
    MusicEventState,
    TailedRecord,
    build_decision_prompt,
    parse_decision,
)


def _install_astrbot_stubs() -> None:
    astrbot = ModuleType("astrbot")
    astrbot.__path__ = []
    api = ModuleType("astrbot.api")
    event = ModuleType("astrbot.api.event")
    components = ModuleType("astrbot.api.message_components")
    star = ModuleType("astrbot.api.star")

    class Logger:
        def warning(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

    class Filter:
        @staticmethod
        def on_llm_request(*args, **kwargs):
            return lambda function: function

        @staticmethod
        def command(*args, **kwargs):
            return lambda function: function

        @staticmethod
        def llm_tool(*args, **kwargs):
            return lambda function: function

    class MessageChain:
        def __init__(self, chain=None):
            self.chain = chain or []

    class Plain:
        def __init__(self, text):
            self.text = text

    class Star:
        def __init__(self, context):
            self.context = context

    api.logger = Logger()
    event.AstrMessageEvent = type("AstrMessageEvent", (), {})
    event.MessageChain = MessageChain
    event.filter = Filter()
    components.Plain = Plain
    star.Context = type("Context", (), {})
    star.Star = Star
    star.register = lambda *args, **kwargs: lambda cls: cls
    sys.modules.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.message_components": components,
        "astrbot.api.star": star,
    })


_install_astrbot_stubs()
from astrbot_plugin_music_companion.main import MusicCompanionPlugin


def context(**overrides):
    value = {
        "title": "测试歌曲",
        "artist": "测试歌手",
        "album": "测试专辑",
        "playback_status": "playing",
        "position_seconds": None,
        "position_source": "unavailable",
        "duration_seconds": None,
        "source_app_id": "cloudmusic",
        "available": True,
    }
    value.update(overrides)
    return value


def item(event_type="track_changed", *, kind="music_context_event", logged="2026-11-07T00:00:00+00:00"):
    record = {
        "kind": kind,
        "type": event_type,
        "logged_at_utc": logged,
        "observed_at_monotonic": 12.5,
        "music_context": context(),
    }
    if kind == "event":
        record["detected_at_s"] = 12.5
        record["evidence"] = {"delta_db": 7.2}
    return TailedRecord(f"audio_events_test.jsonl:{logged}:{event_type}", record)


def append_record(path: Path, record: dict) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")
        file.flush()


class JsonlTailerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "audio_events_20261107_000000_000000.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def test_skips_history_and_waits_for_complete_line(self):
        append_record(self.path, {"kind": "session"})
        tailer = JsonlTailer(self.directory)
        self.assertEqual(tailer.poll().records, [])
        encoded = (json.dumps(item().record, ensure_ascii=False) + "\n").encode("utf-8")
        with self.path.open("ab") as file:
            file.write(encoded[:15])
            file.flush()
        self.assertEqual(tailer.poll().records, [])
        with self.path.open("ab") as file:
            file.write(encoded[15:])
            file.flush()
        batch = tailer.poll()
        self.assertEqual(len(batch.records), 1)
        self.assertEqual(batch.records[0].record["type"], "track_changed")
        tailer.close()

    def test_new_log_does_not_replay_its_history(self):
        append_record(self.path, {"kind": "session"})
        tailer = JsonlTailer(self.directory)
        tailer.poll()
        rotated = self.directory / "audio_events_20261107_000001_000000.jsonl"
        append_record(rotated, item("track_observed").record)
        self.assertEqual(tailer.poll().records, [])
        append_record(rotated, item().record)
        batch = tailer.poll()
        self.assertEqual([entry.record["type"] for entry in batch.records], ["track_changed"])
        tailer.close()

    def test_malformed_line_does_not_block_following_record(self):
        append_record(self.path, {"kind": "session"})
        tailer = JsonlTailer(self.directory)
        tailer.poll()
        with self.path.open("ab") as file:
            file.write(b"not-json\n")
        append_record(self.path, item().record)
        batch = tailer.poll()
        self.assertEqual(len(batch.records), 1)
        self.assertEqual(len(batch.errors), 1)
        tailer.close()

    def test_non_finite_constant_is_null_in_decision_prompt(self):
        append_record(self.path, {"kind": "session"})
        tailer = JsonlTailer(self.directory)
        tailer.poll()
        with self.path.open("ab") as file:
            file.write(b'{"kind":"event","type":"energy_rise","evidence":{"delta_db":NaN}}\n')
        batch = tailer.poll()
        self.assertEqual(len(batch.records), 1)
        self.assertIn('"delta_db": null', build_decision_prompt(batch.records))
        tailer.close()


class MusicEventStateTests(unittest.TestCase):
    def test_track_observation_updates_snapshot_without_calling_agent(self):
        state = MusicEventState(coalesce_seconds=2, cooldown_seconds=45)
        state.ingest([item("track_observed")], 0)
        self.assertEqual(state.due_batch(3), [])
        prompt = state.context_prompt(4)
        self.assertIn("测试歌曲", prompt)
        self.assertIn("不保证等于", prompt)
        self.assertIn("不得推算", prompt)

    def test_coalesces_events_and_ignores_capture_gap(self):
        state = MusicEventState(coalesce_seconds=2, cooldown_seconds=45)
        state.ingest([item("capture_gap", kind="event")], 0)
        state.ingest([item(), item("energy_rise", kind="event")], 0.5)
        self.assertEqual(state.due_batch(1), [])
        batch = state.due_batch(3)
        self.assertEqual([entry.record["type"] for entry in batch], ["track_changed", "energy_rise"])
        prompt = build_decision_prompt(batch)
        self.assertIn("track_changed", prompt)
        self.assertIn("delta_db", prompt)

    def test_duplicate_and_cooldown_do_not_repeat_model_batches(self):
        state = MusicEventState(coalesce_seconds=2, cooldown_seconds=45)
        duplicate = item()
        state.ingest([duplicate, duplicate], 0)
        self.assertEqual(len(state.due_batch(2)), 1)
        state.ingest([item("energy_rise", kind="event", logged="2026-11-07T00:00:03+00:00")], 3)
        self.assertEqual(state.due_batch(5), [])
        state.ingest([item("playback_changed", logged="2026-11-07T00:00:46+00:00")], 46)
        self.assertEqual(len(state.due_batch(48)), 1)

    def test_track_change_is_not_swallowed_by_acoustic_cooldown(self):
        state = MusicEventState(coalesce_seconds=2, cooldown_seconds=45)
        state.ingest([item()], 0)
        self.assertEqual(len(state.due_batch(2)), 1)
        state.ingest([item("energy_rise", kind="event", logged="2026-11-07T00:00:03+00:00")], 3)
        self.assertEqual(state.due_batch(5), [])
        state.ingest([item("track_changed", logged="2026-11-07T00:00:10+00:00")], 10)
        batch = state.due_batch(12)
        self.assertEqual([entry.record["type"] for entry in batch], ["track_changed"])

    def test_stale_snapshot_is_not_injected_as_current(self):
        state = MusicEventState()
        state.ingest([item("track_observed")], 0)
        self.assertEqual(state.context_prompt(301), "")


class DecisionTests(unittest.TestCase):
    def test_accepts_only_structured_send_decision(self):
        self.assertEqual(parse_decision('```json\n{"send":true,"message":" 这首歌\\n  换个口味？"}\n```'), (True, "这首歌 换个口味？"))
        self.assertEqual(parse_decision('{"send":false,"message":"ignored"}'), (False, ""))

    def test_invalid_or_empty_decision_fails_closed(self):
        for text in ("", "send=true", '{"send":"true","message":"x"}', '{"send":true,"message":" "}', "[]"):
            with self.subTest(text=text):
                self.assertEqual(parse_decision(text), (False, ""))


class FakeProvider:
    def __init__(self, completion='{"send":true,"message":"这段变化挺明显，要一起听听吗？"}', error=None):
        self.completion = completion
        self.error = error
        self.calls = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(completion_text=self.completion)


class FakeContext:
    def __init__(self, provider):
        self.provider = provider
        self.sent = []
        self.send_result = True

    async def get_using_provider_async(self, umo=None):
        self.provider.selected_umo = umo
        return self.provider

    def get_provider_by_id(self, provider_id):
        return self.provider if provider_id == "configured" else None

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return self.send_result


class PluginIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "audio_events_20261107_000000_000000.jsonl"
        append_record(self.path, {"kind": "session"})
        self.provider = FakeProvider()
        self.context = FakeContext(self.provider)

    def tearDown(self):
        self.temporary.cleanup()

    def plugin(self, **overrides):
        config = {
            "enabled": True,
            "log_dir": str(self.directory),
            "proactive_enabled": True,
            "target_umo": "platform:FriendMessage:confirmed-user",
            "proactive_cooldown_seconds": 45,
            "llm_timeout_seconds": 12,
        }
        config.update(overrides)
        return MusicCompanionPlugin(self.context, config)

    async def poll_at(self, plugin, *times):
        for value in times:
            with patch("astrbot_plugin_music_companion.main.time.monotonic", return_value=value):
                await plugin._poll_once()

    async def test_event_to_llm_to_qq_uses_one_bounded_text_call(self):
        plugin = self.plugin()
        await self.poll_at(plugin, 0)
        append_record(self.path, item().record)
        append_record(self.path, item("energy_rise", kind="event", logged="2026-11-07T00:00:01+00:00").record)
        with patch.object(MusicCompanionPlugin, "_companion_scene_prompt", return_value="当前陪伴场景"):
            await self.poll_at(plugin, 1, 3)
        self.assertEqual(len(self.provider.calls), 1)
        call = self.provider.calls[0]
        self.assertIn("track_changed", call["prompt"])
        self.assertIn("energy_rise", call["prompt"])
        self.assertIn("当前陪伴场景", call["system_prompt"])
        self.assertNotIn("audio_urls", call)
        self.assertNotIn("image_urls", call)
        self.assertEqual(self.provider.selected_umo, "platform:FriendMessage:confirmed-user")
        self.assertEqual(len(self.context.sent), 1)
        umo, chain = self.context.sent[0]
        self.assertEqual(umo, "platform:FriendMessage:confirmed-user")
        self.assertEqual(chain.chain[0].text, "这段变化挺明显，要一起听听吗？")
        plugin.tailer.close()

    async def test_missing_target_never_resolves_provider_or_sends(self):
        plugin = self.plugin(target_umo="")
        await self.poll_at(plugin, 0)
        append_record(self.path, item().record)
        await self.poll_at(plugin, 1, 3)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.context.sent, [])
        plugin.tailer.close()

    async def test_model_decline_error_and_invalid_json_all_fail_closed(self):
        plugin = self.plugin()
        for provider in (
            FakeProvider('{"send":false,"message":""}'),
            FakeProvider("not-json"),
            FakeProvider(error=RuntimeError("simulated")),
        ):
            with self.subTest(provider=provider):
                context = FakeContext(provider)
                plugin.context = context
                await plugin._analyze_and_send([item()])
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(context.sent, [])

    async def test_non_text_provider_is_rejected_before_model_call(self):
        plugin = self.plugin()
        context = FakeContext(SimpleNamespace())
        plugin.context = context
        await plugin._analyze_and_send([item()])
        self.assertEqual(context.sent, [])

    async def test_normal_qq_request_receives_only_bounded_snapshot(self):
        plugin = self.plugin(proactive_enabled=False)
        with patch("astrbot_plugin_music_companion.main.time.monotonic", return_value=10):
            plugin.state.ingest([item("track_observed")], 10)
        req = SimpleNamespace(system_prompt="既有人格")
        with patch("astrbot_plugin_music_companion.main.time.monotonic", return_value=11):
            await plugin.inject_music_context(None, req)
        self.assertIn("既有人格", req.system_prompt)
        self.assertIn("测试歌曲", req.system_prompt)
        self.assertIn("position_seconds", req.system_prompt)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.context.sent, [])
        plugin.tailer.close()


if __name__ == "__main__":
    unittest.main()
