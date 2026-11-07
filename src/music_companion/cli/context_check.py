"""一次性 GSMTC 诊断：在 Windows 上打开网易云音乐后运行 music-context。"""
from __future__ import annotations

import asyncio
import json
import sys

from ..music_context import GSMTCUnavailableError, MusicContextReader


async def main() -> int:
    try:
        reader = await MusicContextReader.create()
    except GSMTCUnavailableError as exc:
        print(json.dumps({"available": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    context = await reader.read()
    print(json.dumps(context.to_dict(), ensure_ascii=False, indent=2))
    return 0 if context.available else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
