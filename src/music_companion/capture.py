"""Windows 辅助：定位网易云主进程、编译/查找 ProcessLoopback 捕获器。

只做本地进程枚举与 .NET Framework 编译，不联网、不读写用户数据。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

PACKAGE_DIR = Path(__file__).resolve().parent
NATIVE_DIR = PACKAGE_DIR / "native"


def find_cloudmusic_pid() -> int:
    """返回网易云音乐主进程 PID（进程树根）。"""
    command = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
        "@(Get-CimInstance Win32_Process -Filter \"Name='cloudmusic.exe'\" | "
        "Select-Object ProcessId,ParentProcessId) | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True, encoding="utf-8-sig", check=True, timeout=20,
    )
    rows = json.loads(result.stdout) if result.stdout.strip() else []
    if isinstance(rows, dict):
        rows = [rows]
    ids = {row["ProcessId"] for row in rows}
    roots = [row["ProcessId"] for row in rows if row["ParentProcessId"] not in ids]
    if not roots:
        raise RuntimeError("未找到网易云音乐，请先启动客户端并播放歌曲。")
    if len(roots) != 1:
        raise RuntimeError(f"发现多个网易云主进程 {roots}，请用 --pid 指定目标。")
    return roots[0]


def ensure_helper() -> Path:
    """返回已编译的 ProcessLoopback.exe；缺失或过期时用系统 csc 重新编译。"""
    source = NATIVE_DIR / "ProcessLoopback.cs"
    executable = NATIVE_DIR / "ProcessLoopback.exe"
    if not executable.exists() or source.stat().st_mtime > executable.stat().st_mtime:
        compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if not compiler.is_file():
            raise RuntimeError("未找到系统 .NET Framework x64 C# 编译器。")
        subprocess.run(
            [str(compiler), "-nologo", "-platform:x64", "-optimize+", f"-out:{executable}", str(source)],
            check=True, timeout=30, stdout=sys.stderr,
        )
    return executable
