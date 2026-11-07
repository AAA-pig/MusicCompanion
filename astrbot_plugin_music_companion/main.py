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
    parse_decision,
)


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

    async def terminate(self) -> None:
        self._stopping.set()
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            await asyncio.gather(self._poll_task, return_exceptions=True)
        self.tailer.close()

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
