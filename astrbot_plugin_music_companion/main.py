from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
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
    observation_from_record,
    parse_decision,
)

try:  # 记忆层是独立包；未安装时整体降级，不影响原有事件链路。
    from music_companion.music_memory import MusicMemory
    from music_companion.music_playlog import PlayLogRecorder, TrackObservation
except ImportError:  # pragma: no cover - 取决于部署方式
    MusicMemory = None  # type: ignore[assignment]


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
        self.memory_enabled = bool(config.get("memory_enabled", True)) and MusicMemory is not None
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
        db_path = Path(self._memory_db) if self._memory_db else self.log_dir / "music.db"
        try:
            self.memory = MusicMemory(db_path, session_gap_seconds=self.session_gap_seconds)
            self.recorder = PlayLogRecorder(self.memory)
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
        for item in records:
            mapped = observation_from_record(item.record)
            if mapped is None:
                continue
            try:
                written = self.recorder.observe(
                    TrackObservation(
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
