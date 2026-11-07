from __future__ import annotations

import asyncio
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
        self.state = MusicEventState(cooldown_seconds=cooldown)
        self.tailer = JsonlTailer(Path(str(config.get("log_dir") or ".")))
        self._stopping = asyncio.Event()
        self._poll_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        if self.enabled and (self._poll_task is None or self._poll_task.done()):
            self._stopping.clear()
            self._poll_task = asyncio.create_task(self._poll_loop())

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
        if not records or not self.enabled or not self.proactive_enabled:
            return
        if not self.target_umo:
            logger.warning("[MusicCompanion] 已丢弃主动候选：未配置目标 UMO")
            return
        try:
            await asyncio.wait_for(self._analyze_and_send(records), timeout=27)
        except TimeoutError:
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
        if provider is None:
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
            logger.warning("[MusicCompanion] 音乐决策模型超时，本批事件不重试")
            return
        except Exception as exc:
            logger.warning("[MusicCompanion] 音乐决策模型失败：%s", str(exc)[:180])
            return

        should_send, message = parse_decision(str(getattr(response, "completion_text", "") or ""))
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
            if sent is False:
                logger.warning("[MusicCompanion] QQ 发送未成功，不自动重试")
        except TimeoutError:
            logger.warning("[MusicCompanion] QQ 发送超时，结果未知且不自动重试")
        except Exception as exc:
            logger.warning("[MusicCompanion] QQ 发送失败：%s", str(exc)[:180])

    @filter.on_llm_request()
    async def inject_music_context(self, event: AstrMessageEvent, req: Any) -> None:
        if not self.enabled:
            return
        section = self.state.context_prompt(time.monotonic())
        if section:
            req.system_prompt = str(getattr(req, "system_prompt", "") or "") + "\n\n" + section
