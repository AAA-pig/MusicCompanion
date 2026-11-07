"""Capture only NetEase Cloud Music's process tree; all files stay local.

Python 3.10+ / Windows 11. No pip packages needed.
  python test_netease_audio.py                     # capture 10 seconds
  python test_netease_audio.py --seconds 15        # up to 30 seconds
  python test_netease_audio.py --analyze clip.wav   # no recording

No microphone capture, client modification, login access, or uploads.
A detected signal is NOT proof of music content, song identity, or sync accuracy.
"""
from __future__ import annotations

import argparse
import array
from datetime import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import wave

BASE = Path(__file__).resolve().parent


def find_cloudmusic_pid() -> int:
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
    source = BASE / "ProcessLoopback.cs"
    executable = BASE / "ProcessLoopback.exe"
    if not executable.exists() or source.stat().st_mtime > executable.stat().st_mtime:
        compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if not compiler.is_file():
            raise RuntimeError("未找到系统 .NET Framework x64 C# 编译器。")
        subprocess.run(
            [str(compiler), "-nologo", "-platform:x64", "-optimize+", f"-out:{executable}", str(source)],
            check=True, timeout=30, stdout=sys.stderr,
        )
    return executable


def dbfs(value: float) -> float | None:
    return round(20 * math.log10(value), 2) if value > 0 else None


def analyze(path: Path) -> dict:
    with wave.open(str(path), "rb") as wav:
        rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
        frames = wav.getnframes()
        if width != 2 or wav.getcomptype() != "NONE":
            raise ValueError("分析器只支持未压缩 PCM16 WAV。")
        if frames * channels * width > 128 * 1024 * 1024:
            raise ValueError("测试分析器只接受小于 128 MiB 的音频。")
        raw = wav.readframes(frames)
    if len(raw) != frames * channels * width:
        raise ValueError("WAV 数据比文件头声明的帧数短，文件可能未写完。")
    samples = array.array("h", raw)
    if sys.byteorder != "little":
        samples.byteswap()
    count = len(samples)
    peak = max((abs(x) for x in samples), default=0) / 32768
    rms = math.sqrt(sum(x * x for x in samples) / count) / 32768 if count else 0
    threshold = 10 ** (-65 / 20)
    windows = []
    hop = rate * channels
    for start in range(0, count, hop):
        chunk = samples[start:start + hop]
        level = math.sqrt(sum(x * x for x in chunk) / len(chunk)) / 32768
        windows.append({"start_seconds": start / hop, "rms_dbfs": dbfs(level)})
    clipped = sum(x >= 32767 or x <= -32768 for x in samples)
    if not count:
        status = "no_audio_frames"
    elif rms < threshold:
        status = "near_silence"
    else:
        status = "signal_detected"
    return {
        "file": str(path.resolve()), "sample_rate": rate, "channels": channels,
        "bits_per_sample": width * 8, "frames": frames, "duration_seconds": frames / rate,
        "rms_dbfs": dbfs(rms), "peak_dbfs": dbfs(peak),
        "clipped_samples": clipped, "clipped_fraction": clipped / count if count else 0,
        "signal_status": status, "near_silence_threshold_dbfs": -65,
        "per_second_levels": windows,
        "limitations": [
            "信号阈值是测试启发式；检测到信号不等于已经识别出音乐或歌名。",
            "进程包含过滤已启用，但本测试未进行其它应用声音的对照隔离实验。",
            "音频包到达间隔不是从扬声器发声到AI开口的端到端延迟。",
        ],
    }


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="本地网易云进程级录音测试，不上传音频。")
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--pid", type=int, help="目标 cloudmusic 主进程 ID；默认自动选择")
    parser.add_argument("--analyze", type=Path, help="只分析已有 WAV，不录音")
    args = parser.parse_args()
    if args.analyze:
        print(json.dumps(analyze(args.analyze), ensure_ascii=False, indent=2))
        return 0
    if sys.platform != "win32":
        parser.error("进程级捕获需要 Windows。")
    if not math.isfinite(args.seconds) or not 1 <= args.seconds <= 30:
        parser.error("--seconds 必须介于 1 和 30 之间。")
    pid = args.pid if args.pid is not None else find_cloudmusic_pid()
    executable = ensure_helper()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output = BASE / f"netease_{stamp}.wav"
    print(f"即将录制网易云进程树 PID={pid}，时长 {args.seconds:g} 秒。请确认音乐正在播放。", flush=True)
    print("仅存本地，不录麦克风；不会自动启动或改变音乐播放。", flush=True)
    result = subprocess.run(
        [str(executable), str(pid), str(args.seconds), str(output)],
        capture_output=True, encoding="utf-8-sig", timeout=args.seconds + 20,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"捕获器退出码 {result.returncode}")
    capture = json.loads(result.stdout.strip())
    report = analyze(output)
    report["capture"] = capture
    report_path = output.with_suffix(".json")
    with report_path.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"分析报告：{report_path}")
    if report["signal_status"] != "signal_detected":
        print("接口已运行，但没有足够强的音频信号；请确认播放、网易云音量及输出设置后复测。")
        return 4
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"测试未完成：{error}", file=sys.stderr)
        raise SystemExit(1)

