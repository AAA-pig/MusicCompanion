from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

from .bridge import (
    DECISION_SYSTEM_PROMPT,
    JsonlTailer,
    MusicEventState,
    build_decision_prompt,
    format_note_lines,
    format_track_lines,
    observation_from_record,
    parse_decision,
)


def _import_memory_layer(log_dir: Path) -> tuple[Any, Any, Any]:
    """记忆层是独立包。先直接导入，失败时从日志目录附近的 src 兜底。"""

    def attempt() -> tuple[Any, Any, Any]:
        from music_companion.music_memory import MusicMemory
        from music_companion.music_playlog import PlayLogRecorder, TrackObservation

        return MusicMemory, PlayLogRecorder, TrackObservation

    try:
        return attempt()
    except ImportError:
        pass
    for base in (log_dir, log_dir.parent, log_dir.parent.parent):
        src = base / "src"
        if not (src / "music_companion" / "music_memory.py").is_file():
            continue
        entry = str(src)
        if entry not in sys.path:
            sys.path.insert(0, entry)
        try:
            return attempt()
        except ImportError:
            return None, None, None
    return None, None, None


def _observed_at(value: Any) -> datetime:
    """Prefer the log timestamp; fall back to now so a clock skew never crashes."""
    text = str(value or "").strip()
    if text:
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


@register(
    "astrbot_plugin_music_companion",
    "local",
    "将本机网易云结构化音乐事件接入 AstrBot 的 LLM 与 QQ 闭环",
    "0.1.0",
)
class MusicCompanionPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context)
        self.enabled = bool(config.get("enabled", True))
        self.proactive_enabled = bool(config.get("proactive_enabled", False))
        self.target_umo = str(config.get("target_umo") or "").strip()
        self.provider_id = str(config.get("provider_id") or "").strip()
        self.llm_timeout = min(25, max(2, float(config.get("llm_timeout_seconds", 12))))
        cooldown = min(900, max(15, float(config.get("proactive_cooldown_seconds", 45))))
        self.cooldown_seconds = cooldown
        self.state = MusicEventState(cooldown_seconds=cooldown)
        self.log_dir = Path(str(config.get("log_dir") or "."))
        self.tailer = JsonlTailer(self.log_dir)
        self._trace_path = self.log_dir / "music_companion_plugin_trace.jsonl"
        self._skip_trace_reason: str | None = None
        self._stopping = asyncio.Event()
        self._poll_task: asyncio.Task | None = None
        self._memory_layer = _import_memory_layer(self.log_dir)
        self.memory_enabled = (
            bool(config.get("memory_enabled", True)) and self._memory_layer[0] is not None
        )
        self.session_gap_seconds = float(config.get("session_gap_seconds", 300))
        self._memory_db = str(config.get("memory_db") or "").strip()
        self.memory: Any = None
        self.recorder: Any = None

    def _trace(self, event: str, **fields: Any) -> None:
        try:
            line = {
                "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": event,
                **fields,
            }
            with self._trace_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _trace_skip(self, reason: str) -> None:
        if self._skip_trace_reason != reason:
            self._skip_trace_reason = reason
            self._trace("candidate_skipped", reason=reason)

    async def initialize(self) -> None:
        if self.enabled and (self._poll_task is None or self._poll_task.done()):
            self._stopping.clear()
            self._poll_task = asyncio.create_task(self._poll_loop())
        self._open_memory()
        self._trace(
            "startup",
            enabled=self.enabled,
            proactive_enabled=self.proactive_enabled,
            target_umo=self.target_umo,
            provider_id=self.provider_id,
            cooldown_seconds=self.cooldown_seconds,
            llm_timeout_seconds=self.llm_timeout,
            log_dir=str(self.log_dir),
        )

    def _open_memory(self) -> None:
        if not self.memory_enabled or self.recorder is not None:
            return
        memory_cls, recorder_cls, _ = self._memory_layer
        if memory_cls is None or recorder_cls is None:
            return
        db_path = Path(self._memory_db) if self._memory_db else self.log_dir / "music.db"
        try:
            self.memory = memory_cls(db_path, session_gap_seconds=self.session_gap_seconds)
            self.recorder = recorder_cls(self.memory)
            self._trace("memory_ready", db_path=str(db_path))
        except Exception as exc:
            self.memory = None
            self.recorder = None
            self._trace("memory_failed", error=str(exc)[:120])
            logger.warning("[MusicCompanion] 音乐记忆层不可用：%s", str(exc)[:180])

    def _feed_memory(self, records: list[Any]) -> None:
        """把音乐上下文快照喂给记忆层。采集中断不参与判定，也不进记忆。"""
        if self.recorder is None:
            return
        observation_cls = self._memory_layer[2]
        if observation_cls is None:
            return
        for item in records:
            mapped = observation_from_record(item.record)
            if mapped is None:
                continue
            try:
                written = self.recorder.observe(
                    observation_cls(
                        observed_at=_observed_at(mapped["logged_at_utc"]),
                        title=mapped["title"],
                        artist=mapped["artist"],
                        album=mapped["album"],
                        position_seconds=mapped["position_seconds"],
                        duration_seconds=mapped["duration_seconds"],
                        playback_status=mapped["playback_status"],
                    )
                )
            except Exception as exc:
                self._trace("memory_error", error=str(exc)[:120])
                continue
            for text in written:
                self._trace("memory_note", text=text[:200])

    async def terminate(self) -> None:
        self._stopping.set()
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
        self.tailer.close()
        if self.recorder is not None:
            try:
                for text in self.recorder.flush():
                    self._trace("memory_note", text=text[:200], at="terminate")
            except Exception as exc:
                logger.warning("[MusicCompanion] 收尾写入音乐记忆失败：%s", str(exc)[:180])
        if self.memory is not None:
            try:
                self.memory.close()
            except Exception:
                pass
            self.memory = None
            self.recorder = None

    async def _poll_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[MusicCompanion] 音乐事件轮询失败：%s", str(exc)[:180])
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=1)
            except TimeoutError:
                pass

    async def _poll_once(self) -> None:
        batch = self.tailer.poll()
        for error in batch.errors:
            logger.warning("[MusicCompanion] %s", error)
        now = time.monotonic()
        self.state.ingest(batch.records, now)
        self._feed_memory(batch.records)
        records = self.state.due_batch(now)
        if not records:
            return
        if not self.enabled or not self.proactive_enabled:
            self._trace_skip("disabled")
            return
        if not self.target_umo:
            self._trace_skip("no_target_umo")
            logger.warning("[MusicCompanion] 已丢弃主动候选：未配置目标 UMO")
            return
        self._skip_trace_reason = None
        self._trace(
            "candidate_batch",
            count=len(records),
            types=[str(record.record.get("type") or "") for record in records],
        )
        try:
            await asyncio.wait_for(self._analyze_and_send(records), timeout=27)
        except TimeoutError:
            self._trace("chain_timeout")
            logger.warning("[MusicCompanion] 主动音乐链路超时，本批事件不重试")

    async def _get_provider(self) -> Any | None:
        if self.provider_id:
            return self.context.get_provider_by_id(self.provider_id)
        return await self.context.get_using_provider_async(umo=self.target_umo)

    @staticmethod
    def _companion_scene_prompt() -> str:
        try:
            from data.plugins.astrbot_plugin_private_companion.main import (
                get_private_companion_api,
            )

            api = get_private_companion_api()
            if api is None:
                return ""
            context = api.get_realtime_context("", purpose="music")
            return str(context.get("prompt") or "")[:2400]
        except Exception:
            return ""

    async def _analyze_and_send(self, records: list[Any]) -> None:
        provider = await self._get_provider()
        if provider is None or not callable(getattr(provider, "text_chat", None)):
            self._trace("candidate_dropped", reason="no_text_provider")
            logger.warning("[MusicCompanion] 已丢弃主动候选：目标会话没有可用文本 Provider")
            return
        scene_prompt = self._companion_scene_prompt()
        system_prompt = DECISION_SYSTEM_PROMPT
        if scene_prompt:
            system_prompt += "\n\n以下是陪伴宿主当前场景，只取与音乐事件相关的内容：\n" + scene_prompt
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=build_decision_prompt(records),
                    system_prompt=system_prompt,
                ),
                timeout=self.llm_timeout,
            )
        except TimeoutError:
            self._trace("candidate_dropped", reason="model_timeout")
            logger.warning("[MusicCompanion] 音乐决策模型超时，本批事件不重试")
            return
        except Exception as exc:
            self._trace("candidate_dropped", reason="model_error", error=str(exc)[:120])
            logger.warning("[MusicCompanion] 音乐决策模型失败：%s", str(exc)[:180])
            return

        raw_text = str(getattr(response, "completion_text", "") or "")
        should_send, message = parse_decision(raw_text)
        self._trace(
            "decision",
            send=should_send,
            message_len=len(message),
            raw_len=len(raw_text),
        )
        if not should_send:
            return
        try:
            sent = await asyncio.wait_for(
                self.context.send_message(
                    self.target_umo,
                    MessageChain(chain=[Plain(message)]),
                ),
                timeout=1,
            )
            self._trace("send_result", ok=bool(sent))
            if sent is False:
                logger.warning("[MusicCompanion] QQ 发送未成功，不自动重试")
        except TimeoutError:
            self._trace("send_result", ok=None, reason="timeout")
            logger.warning("[MusicCompanion] QQ 发送超时，结果未知且不自动重试")
        except Exception as exc:
            self._trace("send_result", ok=False, reason="error", error=str(exc)[:120])
            logger.warning("[MusicCompanion] QQ 发送失败：%s", str(exc)[:180])

    @filter.on_llm_request()
    async def inject_music_context(self, event: AstrMessageEvent, req: Any) -> None:
        """向普通 QQ 对话注入最近音乐快照，不额外调用模型。"""
        if not self.enabled:
            return
        section = self.state.context_prompt(time.monotonic())
        if section:
            req.system_prompt = str(getattr(req, "system_prompt", "") or "") + "\n\n" + section

    # ------------------------------------------------------------- memory read

    def _music_memory_text(self, action: str) -> str:
        if self.memory is None:
            return "音乐记忆还没打开，记忆层没加载上呢。"
        key = action.strip().lower()
        if key in {"日记", "今天", "今日", "diary"}:
            return "今天的听歌日记：\n" + self.memory.build_diary()
        if key in {"常听", "最爱", "喜欢", "favourite", "favorite"}:
            lines = format_track_lines(self.memory.favourite_tracks(limit=5), limit=5)
            return "常听的：\n" + "\n".join(lines) if lines else "还没听出常听的歌呢。"
        if key in {"跳过", "不爱", "skipped"}:
            lines = format_track_lines(
                self.memory.skipped_tracks(limit=5), limit=5, mode="skipped"
            )
            return "老被跳过的：\n" + "\n".join(lines) if lines else "还没有老被跳过的歌。"
        lines = format_note_lines(self.memory.recent_notes(limit=5), limit=5)
        if not lines:
            return "音乐记忆还是空的，最近没听出什么。"
        return "最近：\n" + "\n".join(lines)

    @filter.command("音乐记忆")
    async def cmd_music_memory(self, event: AstrMessageEvent):
        """音乐记忆：/音乐记忆 [常听|跳过|日记|最近]"""
        if self.target_umo and event.unified_msg_origin != self.target_umo:
            yield event.plain_result("这个只在本机主人的会话里开哦。")
            return
        raw = (event.get_message_str() or "").strip()
        parts = raw.split(None, 1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        try:
            text = self._music_memory_text(arg)
        except Exception as exc:
            logger.warning("[MusicCompanion] 读取音乐记忆失败：%s", str(exc)[:180])
            text = "记忆读失败了，等会儿再翻一次吧。"
        yield event.plain_result(text)

    @filter.llm_tool(name="music_companion_recall")
    async def music_companion_recall_tool(
        self,
        event: AstrMessageEvent,
        action: str = "recent",
        limit: int = 5,
    ) -> str:
        """查询本机最近的听歌行为记忆（听过什么、听多久、跳过哪些）。
        只含可数的行为记录，不还原音频、歌词或播放内容；没有记录时如实返回空。

        Args:
            action(string): recent / favourite / skipped / diary / digest 之一，默认 recent
            limit(int): 最多返回几条，默认 5，最多 10
        """
        if self.memory is None:
            return json.dumps(
                {"ok": False, "error": "memory_unavailable", "hint": "记忆层没有加载，不要编造听歌记录。"},
                ensure_ascii=False,
            )
        count = max(1, min(10, int(limit or 5)))
        key = str(action or "recent").strip().lower()
        try:
            if key in {"diary", "日记"}:
                payload = {"ok": True, "action": "diary", "text": self.memory.build_diary()}
            elif key in {"favourite", "favorite", "常听"}:
                payload = {
                    "ok": True,
                    "action": "favourite",
                    "tracks": self.memory.favourite_tracks(limit=count),
                }
            elif key in {"skipped", "跳过"}:
                payload = {
                    "ok": True,
                    "action": "skipped",
                    "tracks": self.memory.skipped_tracks(limit=count),
                }
            elif key == "digest":
                payload = {"ok": True, "action": "digest", "text": self.memory.digest(limit=count)}
            else:
                payload = {
                    "ok": True,
                    "action": "recent",
                    "notes": self.memory.recent_notes(limit=count),
                }
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)[:120]}, ensure_ascii=False)
        return json.dumps(payload, ensure_ascii=False)
