"""Local real-time changes from NetEase process audio or a PCM16 WAV.

Run with: music-monitor --seconds 60   (uv run music-monitor ...)
Replay:   music-monitor --wav recording.wav
Continuous (until Ctrl+C): --seconds 0. Use --json for JSONL stdout.
Default logs contain features/events, not raw audio. No network/API calls.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import struct
import subprocess
import sys
import threading
import time
from typing import BinaryIO
import wave

from .audio_changes import AudioChangeDetector, ChangeEvent, DetectorConfig
from .music_context import MusicContextPoller
from .capture import ensure_helper, find_cloudmusic_pid

STREAM_HEADER = struct.Struct("<4sIII")
PACKET_HEADER = struct.Struct("<IId")
END_FLAG = 0x80000000
LABELS = {
    "audio_started": "检测到声音", "silence_start": "持续安静（可能暂停）",
    "audio_resumed": "声音恢复", "energy_rise": "能量明显上升",
    "energy_drop": "能量明显下降", "texture_change": "音色/编配变化候选",
    "rhythm_change": "节奏密度变化候选", "capture_gap": "采集间断，已重置基线",
    "capture_stalled": "暂未收到音频，不能判断是暂停还是采集异常",
    "capture_recovered": "音频数据恢复",
}


@dataclass
class Packet:
    frames: int
    flags: int
    received_seconds: float
    pcm: bytes
    queued_at: float = 0.0
    sequence: int = 0


def sequence_gap(previous: int | None, current: int) -> bool:
    """Consumer ordering, not a producer's future cumulative drop count."""
    return current != (0 if previous is None else previous + 1)


def read_exact(stream: BinaryIO, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        piece = stream.read(size - len(data))
        if not piece:
            raise EOFError(f"Audio stream truncated: expected {size}, received {len(data)} bytes")
        data.extend(piece)
    return bytes(data)


def read_header(stream: BinaryIO) -> tuple[int, int]:
    magic, rate, channels, bits = STREAM_HEADER.unpack(read_exact(stream, STREAM_HEADER.size))
    if magic != b"NCM1" or rate != 48000 or channels != 2 or bits != 16:
        raise ValueError("Unsupported capture protocol or audio format")
    return rate, channels


def read_packet(stream: BinaryIO, rate: int, channels: int) -> Packet:
    frames, flags, received = PACKET_HEADER.unpack(read_exact(stream, PACKET_HEADER.size))
    if frames > rate or not math.isfinite(received) or received < 0:
        raise ValueError("Invalid capture packet length/timestamp")
    if flags & END_FLAG and frames:
        raise ValueError("End packet must not contain PCM")
    pcm = read_exact(stream, frames * channels * 2) if frames else b""
    return Packet(frames, flags, received, pcm, time.perf_counter())


class Reporter:
    def __init__(self, path: Path, json_stdout: bool) -> None:
        self.path = path
        self.file = path.open("x", encoding="utf-8")
        self.json_stdout = json_stdout
        self.counts: Counter[str] = Counter()

    def write(self, record: dict) -> None:
        record = {"logged_at_utc": datetime.now(timezone.utc).isoformat(), **record}
        text = json.dumps(record, ensure_ascii=False, allow_nan=False)
        self.file.write(text + "\n")
        self.file.flush()
        if self.json_stdout:
            print(text, flush=True)
        elif record["kind"] == "event":
            print(f"[{record['detected_at_s']:7.2f}s] {LABELS.get(record['type'], record['type'])} | {record['evidence']}", flush=True)
        elif record["kind"] == "status":
            print(f"[{record['capture_seconds']:7.2f}s] 状态={record['state']}  RMS={record['features'].get('rms_dbfs', '--')} dBFS", flush=True)
        elif record["kind"] == "music_context_event":
            print(f"音乐上下文：{record['type']} | {record.get('music_context')}", flush=True)
        elif record["kind"] == "summary":
            print("检测结束：" + json.dumps(record, ensure_ascii=False), flush=True)

    def event(self, event: ChangeEvent, music_context: dict | None = None) -> None:
        self.counts[event.type] += 1
        record = {"kind": "event", **event.to_dict()}
        if music_context is not None:
            record["music_context"] = music_context
        self.write(record)

    def close(self) -> None:
        self.file.close()


def replay(path: Path, config: DetectorConfig, reporter: Reporter) -> dict:
    begin = time.perf_counter()
    frames_read = 0
    maximum_block_ms = 0.0
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getcomptype() != "NONE":
            raise ValueError("回放只支持未压缩 PCM16 WAV")
        rate, channels = wav.getframerate(), wav.getnchannels()
        detector = AudioChangeDetector(rate, channels, config)
        reporter.write({"kind": "session", "mode": "wav_replay", "input": str(path.resolve()),
                        "sample_rate": rate, "channels": channels, "config": config.__dict__,
                        "time_basis": "capture_session_seconds", "audio_saved": False})
        while True:
            pcm = wav.readframes(4096)
            if not pcm:
                break
            frames_read += len(pcm) // (2 * channels)
            started = time.perf_counter()
            events = detector.feed_pcm16(pcm)
            maximum_block_ms = max(maximum_block_ms, (time.perf_counter() - started) * 1000)
            for event in events:
                reporter.event(event)
        if frames_read != wav.getnframes():
            raise ValueError("WAV 文件不完整：数据帧数与文件头不符")
    return {"mode": "wav_replay", "audio_seconds": round(frames_read / rate, 4),
            "analysis_seconds": round(time.perf_counter() - begin, 4),
            "max_block_processing_ms": round(maximum_block_ms, 3),
            "feature_frames": detector.feature_frames, "events": dict(reporter.counts)}


def live(seconds: float, pid: int | None, config: DetectorConfig, reporter: Reporter, meter_interval: float,
         *, music_context_enabled: bool = False) -> dict:
    if sys.platform != "win32":
        raise RuntimeError("实时进程捕获需要 Windows")
    pid = pid if pid is not None else find_cloudmusic_pid()
    helper = ensure_helper()
    process: subprocess.Popen | None = None
    packets: queue.Queue[Packet] = queue.Queue(maxsize=64)
    state: dict = {"format": None, "error": None, "done": False, "dropped": 0, "end_seen": False}
    stderr_lines: deque[str] = deque(maxlen=20)
    format_ready = threading.Event()
    stop = threading.Event()

    def reader() -> None:
        try:
            rate, channels = read_header(process.stdout)
            state["format"] = (rate, channels)
            format_ready.set()
            previous_time = -1.0
            next_sequence = 0
            while not stop.is_set():
                packet = read_packet(process.stdout, rate, channels)
                if packet.received_seconds < previous_time:
                    raise ValueError("Capture timestamp moved backwards")
                previous_time = packet.received_seconds
                if packet.flags & END_FLAG:
                    state["end_seen"] = True
                    break
                if not packet.frames:
                    continue  # Heartbeat is not silence and not audio.
                packet.sequence = next_sequence
                next_sequence += 1
                try:
                    packets.put_nowait(packet)
                except queue.Full:
                    try:
                        packets.get_nowait()
                        state["dropped"] += 1
                    except queue.Empty:
                        pass
                    packets.put_nowait(packet)
        except Exception as exc:
            if not stop.is_set():
                state["error"] = exc
        finally:
            format_ready.set()
            state["done"] = True

    def drain_stderr() -> None:
        for line in iter(process.stderr.readline, b""):
            stderr_lines.append(line.decode("utf-8-sig", errors="replace").strip())

    read_thread = threading.Thread(target=reader, daemon=True, name="netease-pcm-reader")
    error_thread = threading.Thread(target=drain_stderr, daemon=True, name="netease-diagnostics")
    detector: AudioChangeDetector | None = None
    start = time.perf_counter()
    last_audio_at = start
    last_meter = start
    first_received: float | None = None
    prior_packet_end: float | None = None
    prior_sequence: int | None = None
    stale_packets = 0
    stale_since_reset = False
    stalled = False
    interrupted = False
    audio_frames = 0
    maximum_processing_ms = 0.0
    context_poller = MusicContextPoller() if music_context_enabled else None

    def report_event(event: ChangeEvent) -> None:
        context = context_poller.latest if context_poller else None
        reporter.event(event, context.to_dict() if context else None)

    def drain_context_events() -> None:
        if context_poller:
            for record in context_poller.drain_events():
                reporter.write(record)

    try:
        process = subprocess.Popen([str(helper), "--stream", str(pid), str(seconds)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        assert process.stdout is not None and process.stderr is not None
        read_thread.start()
        error_thread.start()
        if not format_ready.wait(12):
            raise TimeoutError("音频捕获初始化超时")
        if state["error"]:
            raise RuntimeError(str(state["error"]) + "; " + " | ".join(stderr_lines))
        if state["format"] is None:
            raise RuntimeError("捕获器未提供音频格式")
        rate, channels = state["format"]
        detector = AudioChangeDetector(rate, channels, config)
        start = last_audio_at = last_meter = time.perf_counter()
        reporter.write({"kind": "session", "mode": "live", "target_pid": pid,
                        "capture_scope": "process_tree_include", "sample_rate": rate,
                        "channels": channels, "audio_saved": False,
                        "music_context_requested": music_context_enabled,
                        "time_basis": "capture_session_seconds", "config": config.__dict__})
        if context_poller:
            try:
                context_poller.start()
            except Exception as exc:
                reporter.write({
                    "kind": "music_context_event", "type": "music_context_error",
                    "observed_at_monotonic": time.monotonic(),
                    "music_context": {"available": False, "error": f"{type(exc).__name__}: {exc}"},
                })
                context_poller = None
        while not state["done"] or not packets.empty():
            drain_context_events()
            now = time.perf_counter()
            if seconds and now - start > seconds + 15:
                raise TimeoutError("捕获器超过预期时限")
            try:
                packet = packets.get(timeout=0.1)
            except queue.Empty:
                if not stalled and now - last_audio_at >= 1.0:
                    stalled = True
                    at = max(detector.time_seconds, now - start)
                    report_event(ChangeEvent("capture_stalled", at, at, detector.stream_epoch,
                                             {"reason": "no_pcm_for_1s_not_assumed_silence"}))
                continue
            now = time.perf_counter()
            if now - packet.queued_at > 0.5:
                stale_packets += 1
                stale_since_reset = True
                continue
            reasons = []
            if stale_since_reset:
                reasons.append("stale_packets_discarded")
                stale_since_reset = False
            if sequence_gap(prior_sequence, packet.sequence):
                reasons.append("packet_sequence_gap")
            if packet.flags & 1:
                reasons.append("wasapi_discontinuity")
            if packet.flags & 4:
                reasons.append("wasapi_timestamp_error")
            if prior_packet_end is not None and packet.received_seconds - prior_packet_end > packet.frames / rate + 0.2:
                reasons.append("packet_timing_gap")
            if stalled:
                reasons.append("capture_recovered")
            if first_received is None:
                first_received = packet.received_seconds
                if not reasons:
                    # Anchor the first PCM frame to native receipt time minus its
                    # duration. This is an approximate capture clock, not song time.
                    detector = AudioChangeDetector(rate, channels, config,
                        start_seconds=max(0.0, packet.received_seconds - packet.frames / rate))
            if reasons:
                at = max(detector.time_seconds, packet.received_seconds - packet.frames / rate)
                report_event(detector.reset(at, ";".join(reasons)))
                if stalled:
                    report_event(ChangeEvent("capture_recovered", at, at, detector.stream_epoch))
            stalled = False
            last_audio_at = now
            prior_packet_end = packet.received_seconds
            prior_sequence = packet.sequence
            before = time.perf_counter()
            for event in detector.feed_pcm16(packet.pcm):
                report_event(event)
            maximum_processing_ms = max(maximum_processing_ms, (time.perf_counter() - before) * 1000)
            audio_frames += packet.frames
            if meter_interval > 0 and now - last_meter >= meter_interval:
                reporter.write({"kind": "status", "capture_seconds": round(detector.time_seconds, 4),
                                "state": detector.state, "features": detector.last_features})
                last_meter = now
        read_thread.join(timeout=2)
        process.wait(timeout=3)
        error_thread.join(timeout=2)
        if state["error"]:
            raise RuntimeError(str(state["error"]) + "; " + " | ".join(stderr_lines))
        if process.returncode or not state["end_seen"]:
            raise RuntimeError("捕获器异常结束：" + " | ".join(stderr_lines))
    except KeyboardInterrupt:
        interrupted = True
    finally:
        stop.set()
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        finally:
            # Startup can fail before Popen returns or before either thread starts.
            # Never join an unstarted thread or leave valid child pipes open.
            for thread in (read_thread, error_thread):
                if thread.ident is not None:
                    thread.join(timeout=2)
            if process is not None:
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()
        if context_poller:
            context_poller.stop()
            drain_context_events()
    native = None
    for line in reversed(stderr_lines):
        try:
            native = json.loads(line)
            break
        except ValueError:
            pass
    return {"mode": "live", "interrupted": interrupted, "wall_seconds": round(time.perf_counter() - start, 4),
            "audio_seconds": round(audio_frames / 48000, 4), "first_packet_received_seconds": first_received,
            "queue_dropped_packets": state["dropped"], "stale_packets_discarded": stale_packets,
            "max_packet_processing_ms": round(maximum_processing_ms, 3),
            "feature_frames": detector.feature_frames if detector else 0,
            "events": dict(reporter.counts), "native_capture": native,
            "music_context_requested": music_context_enabled,
            "latency_note": "Processing time is not microphone/speaker-to-AI end-to-end latency."}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="本地网易云音频变化检测：不联网、不上传、不保存原始音频。")
    parser.add_argument("--seconds", type=float, default=60, help="实时检测秒数；0=持续到Ctrl+C，默认60，最长3600")
    parser.add_argument("--pid", type=int, help="网易云主进程ID，默认自动查找")
    parser.add_argument("--wav", type=Path, help="快速分析已有PCM16 WAV，不启动捕获")
    parser.add_argument("--log", type=Path, help="新建JSONL日志路径，默认当前脚本目录；不覆盖已有文件")
    parser.add_argument("--json", action="store_true", help="stdout输出结构化JSONL")
    parser.add_argument("--music-context", action="store_true", help="附加只读GSMTC歌曲上下文；失败时不影响音频检测")
    parser.add_argument("--meter-interval", type=float, default=1, help="实时状态输出间隔，0关闭")
    parser.add_argument("--silence-db", type=float, default=-60, help="静音阈值dBFS；恢复阈值高8dB")
    parser.add_argument("--rise-db", type=float, default=6, help="能量上升阈值dB")
    parser.add_argument("--drop-db", type=float, default=8, help="能量下降阈值dB")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds < 0 or args.seconds > 3600 or (0 < args.seconds < 1):
        parser.error("--seconds 只能为0或1至3600")
    if not math.isfinite(args.meter_interval) or args.meter_interval < 0:
        parser.error("--meter-interval 必须是非负有限数")
    if args.wav and args.music_context:
        parser.error("--music-context 仅用于实时检测")
    reporter = None
    try:
        config = DetectorConfig(silence_dbfs=args.silence_db, resume_dbfs=args.silence_db + 8,
                                energy_rise_db=args.rise_db, energy_drop_db=args.drop_db)
        path = args.log or Path.cwd() / ("audio_events_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".jsonl")
        reporter = Reporter(path, args.json)
        print(f"事件日志：{path.resolve()}\n仅检测声音变化；事件时间不是歌曲播放位置。Ctrl+C 停止。", file=sys.stderr, flush=True)
        summary = replay(args.wav, config, reporter) if args.wav else live(
            args.seconds, args.pid, config, reporter, args.meter_interval,
            music_context_enabled=args.music_context,
        )
        reporter.write({"kind": "summary", **summary})
        return 0
    except (OSError, ValueError, RuntimeError, EOFError, subprocess.SubprocessError) as exc:
        if reporter:
            reporter.write({"kind": "error", "error": str(exc)})
        print(f"检测未完成：{exc}", file=sys.stderr)
        return 1
    finally:
        if reporter:
            reporter.close()


if __name__ == "__main__":
    raise SystemExit(main())

