"""Offline deterministic tests; never open a microphone or start recording."""
from dataclasses import replace
import io
import json
import math
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np

from audio_changes import AudioChangeDetector, DetectorConfig
from monitor_audio import END_FLAG, PACKET_HEADER, STREAM_HEADER, Reporter, live, read_header, read_packet, sequence_gap
from music_context import MusicContext

RATE = 48000


def tone(seconds, amplitude=0.1, frequency=440):
    t = np.arange(round(seconds * RATE)) / RATE
    return (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)


def pulses(seconds, hz):
    t = np.arange(round(seconds * RATE)) / RATE
    envelope = 0.025 + 0.18 * np.exp(-40 * (t % (1 / hz)))
    return (envelope * np.sin(2 * np.pi * 440 * t)).astype(np.float32)


def run_signal(signal, config=None, chunk=2400):
    channels = signal.shape[1] if signal.ndim == 2 else 1
    detector = AudioChangeDetector(RATE, channels, config)
    events = []
    for start in range(0, len(signal), chunk):
        events.extend(detector.feed(signal[start:start + chunk]))
    return detector, events


def changes(events):
    return [e for e in events if e.type in ("energy_rise", "energy_drop", "texture_change", "rhythm_change")]


class DetectorTests(unittest.TestCase):
    def test_silence_once(self):
        _, events = run_signal(np.zeros(RATE * 4, dtype=np.float32))
        self.assertEqual([e.type for e in events], ["silence_start"])
        self.assertAlmostEqual(events[0].detected_at_s, 0.7)

    def test_quantization_noise_is_silent(self):
        rng = np.random.default_rng(7)
        signal = rng.integers(-1, 2, RATE * 2).astype(np.float32) / 32768
        detector, events = run_signal(signal)
        self.assertEqual(detector.state, "silence")
        self.assertEqual([e.type for e in events], ["silence_start"])

    def test_steady_tone_no_changes(self):
        _, events = run_signal(tone(12))
        self.assertEqual([e.type for e in events], ["audio_started"])

    def test_antiphase_stereo_does_not_cancel(self):
        signal = tone(4)
        detector, events = run_signal(np.column_stack((signal, -signal)))
        self.assertEqual(detector.state, "audio")
        self.assertGreater(detector.last_features["rms_dbfs"], -30)
        self.assertEqual([e.type for e in events], ["audio_started"])

    def test_pause_resume(self):
        _, events = run_signal(np.concatenate((tone(4), np.zeros(RATE * 2), tone(4))))
        self.assertEqual([e.type for e in events], ["audio_started", "silence_start", "audio_resumed"])
        self.assertLessEqual(events[1].detected_at_s - 4, 0.8)
        self.assertLessEqual(events[2].detected_at_s - 6, 0.35)

    def test_short_silent_gap_is_not_pause(self):
        _, events = run_signal(np.concatenate((tone(4), np.zeros(round(RATE * 0.2)), tone(4))))
        self.assertNotIn("silence_start", [e.type for e in events])

    def test_energy_rise_and_cooldown(self):
        _, events = run_signal(np.concatenate((tone(6, 0.035), tone(12, 0.24))))
        rises = [e for e in events if e.type == "energy_rise"]
        self.assertEqual(len(rises), 1)
        self.assertGreater(rises[0].evidence["delta_db"], 10)
        self.assertLessEqual(rises[0].detected_at_s - 6, 1.5)

    def test_energy_drop(self):
        _, events = run_signal(np.concatenate((tone(6, 0.24), tone(7, 0.035))))
        drops = [e for e in events if e.type == "energy_drop"]
        self.assertEqual(len(drops), 1)
        self.assertLessEqual(drops[0].detected_at_s - 6, 1.5)

    def test_short_energy_spike_ignored(self):
        _, events = run_signal(np.concatenate((tone(6, 0.04), tone(0.15, 0.4), tone(6, 0.04))))
        self.assertNotIn("energy_rise", [e.type for e in events])

    def test_texture_at_constant_level(self):
        _, events = run_signal(np.concatenate((tone(6, frequency=300), tone(7, frequency=3000))))
        matches = [e for e in events if e.type == "texture_change"]
        self.assertEqual(len(matches), 1)
        self.assertGreater(matches[0].evidence["spectral_distance"], 0.7)
        self.assertLessEqual(matches[0].detected_at_s - 6, 1.5)
        self.assertNotIn("energy_rise", [e.type for e in events])

    def test_rhythm_density_change(self):
        _, events = run_signal(np.concatenate((pulses(6, 1), pulses(8, 4))))
        matches = [e for e in events if e.type == "rhythm_change"]
        self.assertTrue(matches, [e.to_dict() for e in events])
        self.assertLessEqual(matches[0].detected_at_s - 6, 2.5)

    def test_chunk_boundaries_do_not_change_results(self):
        signal = np.concatenate((tone(5, 0.04), tone(4, 0.2), np.zeros(RATE), tone(2)))
        first, a = run_signal(signal, chunk=2400)
        second, b = run_signal(signal, chunk=997)
        self.assertEqual([e.to_dict() for e in a], [e.to_dict() for e in b])
        self.assertAlmostEqual(first.time_seconds, second.time_seconds)

    def test_pcm16_conversion_and_alignment(self):
        detector = AudioChangeDetector(RATE, 2)
        pcm = np.column_stack((tone(1), tone(1))).astype(np.float64)
        values = (pcm * 32767).astype("<i2")
        events = detector.feed_pcm16(values.tobytes())
        self.assertEqual([e.type for e in events], ["audio_started"])
        with self.assertRaises(ValueError):
            detector.feed_pcm16(b"\x00")

    def test_reset_discards_pending_and_rewarms(self):
        detector, _ = run_signal(tone(4, 0.04))
        detector.feed(tone(0.2, 0.3))
        gap = detector.reset(10, "test_gap")
        self.assertEqual(gap.type, "capture_gap")
        self.assertEqual(gap.stream_epoch, 1)
        events = detector.feed(tone(2, 0.3))
        self.assertEqual([e.type for e in events], ["audio_started"])
        self.assertGreater(events[0].detected_at_s, 10)

    def test_nonzero_capture_origin(self):
        detector = AudioChangeDetector(RATE, 1, start_seconds=0.8)
        events = detector.feed(tone(1))
        self.assertAlmostEqual(events[0].detected_at_s, 1.05)
        self.assertAlmostEqual(detector.time_seconds, 1.8)
        with self.assertRaises(ValueError):
            AudioChangeDetector(start_seconds=math.inf)

    def test_reset_does_not_invent_silence(self):
        detector, _ = run_signal(tone(2))
        detector.reset(8)
        self.assertEqual(detector.state, "initializing")
        self.assertEqual(detector.feed_pcm16(b""), [])

    def test_invalid_input(self):
        detector = AudioChangeDetector(RATE, 1)
        for value in (np.array([math.nan]), np.array([math.inf]), np.array([2.0])):
            with self.assertRaises(ValueError):
                detector.feed(value)
        with self.assertRaises(ValueError):
            detector.feed(np.zeros((4, 2)))
        with self.assertRaises(ValueError):
            detector.reset(-1)

    def test_invalid_configuration(self):
        for kwargs in ({"hop_seconds": 0}, {"silence_dbfs": -40, "resume_dbfs": -50},
                       {"energy_rise_db": math.nan}, {"texture_distance": 2},
                       {"history_seconds": 1}, {"minimum_baseline_seconds": 10}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                DetectorConfig(**kwargs)

    def test_cooldown_is_respected(self):
        config = replace(DetectorConfig(), event_cooldown_seconds=20)
        signal = np.concatenate((tone(5, 0.03), tone(2, 0.3), tone(4, 0.03), tone(3, 0.3)))
        _, events = run_signal(signal, config=config)
        self.assertEqual(len([e for e in events if e.type == "energy_rise"]), 1)

    def test_history_is_bounded(self):
        detector, _ = run_signal(tone(15))
        self.assertLessEqual(len(detector._history), 162)
        self.assertLess(len(detector._buffer), detector.hop_frames)


class ProtocolTests(unittest.TestCase):
    def test_sequence_gaps_follow_consumed_order(self):
        self.assertFalse(sequence_gap(None, 0))
        self.assertTrue(sequence_gap(None, 4))
        self.assertFalse(sequence_gap(0, 1))
        self.assertTrue(sequence_gap(1, 6))
        self.assertFalse(sequence_gap(6, 7))
        self.assertTrue(sequence_gap(7, 7))
        self.assertTrue(sequence_gap(7, 5))

    def test_valid_header_and_packet(self):
        stream = io.BytesIO(STREAM_HEADER.pack(b"NCM1", RATE, 2, 16) + PACKET_HEADER.pack(2, 1, 0.02) + b"\0" * 8)
        rate, channels = read_header(stream)
        packet = read_packet(stream, rate, channels)
        self.assertEqual((packet.frames, packet.flags, packet.pcm), (2, 1, b"\0" * 8))

    def test_heartbeat_and_end_have_no_pcm(self):
        for flags in (0, END_FLAG):
            packet = read_packet(io.BytesIO(PACKET_HEADER.pack(0, flags, 1.0)), RATE, 2)
            self.assertEqual(packet.pcm, b"")
            self.assertEqual(packet.frames, 0)

    def test_truncated_packet_fails(self):
        stream = io.BytesIO(PACKET_HEADER.pack(10, 0, 1.0) + b"\0" * 3)
        with self.assertRaises(EOFError):
            read_packet(stream, RATE, 2)

    def test_bad_header_and_packet_fail(self):
        with self.assertRaises(ValueError):
            read_header(io.BytesIO(STREAM_HEADER.pack(b"XXXX", RATE, 2, 16)))
        for frames, flags, timestamp in ((RATE + 1, 0, 1), (1, END_FLAG, 1), (0, 0, math.nan), (0, 0, -1)):
            with self.subTest(frames=frames, flags=flags), self.assertRaises(ValueError):
                read_packet(io.BytesIO(PACKET_HEADER.pack(frames, flags, timestamp)), RATE, 2)

    def test_fragmented_reads(self):
        class Fragmented(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 3))
        stream = Fragmented(STREAM_HEADER.pack(b"NCM1", RATE, 2, 16))
        self.assertEqual(read_header(stream), (RATE, 2))

    def test_live_protocol_without_recording(self):
        pcm = np.column_stack((tone(0.05), tone(0.05)))
        raw = (pcm * 32767).astype("<i2").tobytes()
        data = STREAM_HEADER.pack(b"NCM1", RATE, 2, 16)
        for index in range(10):
            data += PACKET_HEADER.pack(2400, 0, (index + 1) * 0.05) + raw
        data += PACKET_HEADER.pack(0, END_FLAG, 0.5)

        class FakeProcess:
            def __init__(self):
                self.stdout = io.BytesIO(data)
                self.stderr = io.BytesIO(b'{"packets":10}\n')
                self.returncode = 0
            def poll(self): return self.returncode
            def wait(self, timeout=None): return self.returncode
            def terminate(self): self.returncode = -1
            def kill(self): self.returncode = -1

        with tempfile.TemporaryDirectory() as directory:
            reporter = Reporter(Path(directory) / "events.jsonl", True)
            try:
                with patch("monitor_audio.ensure_helper", return_value=Path("fake.exe")), \
                     patch("monitor_audio.subprocess.Popen", return_value=FakeProcess()), \
                     patch("monitor_audio.sys.platform", "win32"), patch("builtins.print"):
                    result = live(1, 123, DetectorConfig(), reporter, 0)
                self.assertEqual(result["audio_seconds"], 0.5)
                self.assertEqual(result["events"], {"audio_started": 1})
                self.assertEqual(result["native_capture"], {"packets": 10})
            finally:
                reporter.close()
            entries = [json.loads(line) for line in (Path(directory) / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(row.get("type") == "audio_started" for row in entries))


class MusicContextIntegrationTests(unittest.TestCase):
    @staticmethod
    def stream_data():
        pcm = np.column_stack((tone(0.05), tone(0.05)))
        raw = (pcm * 32767).astype("<i2").tobytes()
        data = STREAM_HEADER.pack(b"NCM1", RATE, 2, 16)
        for index in range(10):
            data += PACKET_HEADER.pack(2400, 0, (index + 1) * 0.05) + raw
        return data + PACKET_HEADER.pack(0, END_FLAG, 0.5)

    def test_live_attaches_context_and_writes_context_events(self):
        context = MusicContext(
            title="Test Song", artist="Test Artist", playback_status="playing",
            source_app_id="cloudmusic.exe", sampled_at_monotonic=1, available=True,
        )

        class FakePoller:
            latest = context

            def __init__(self):
                self.events = [{
                    "kind": "music_context_event", "type": "track_observed",
                    "music_context": context.to_dict(),
                }]
                self.stopped = False

            def start(self):
                pass

            def stop(self):
                self.stopped = True

            def drain_events(self):
                events, self.events = self.events, []
                return events

        process = LifecycleTests.FakeProcess(self.stream_data())
        poller = FakePoller()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            reporter = Reporter(path, True)
            try:
                with patch("monitor_audio.ensure_helper", return_value=Path("fake.exe")), \
                     patch("monitor_audio.subprocess.Popen", return_value=process), \
                     patch("monitor_audio.MusicContextPoller", return_value=poller), \
                     patch("monitor_audio.sys.platform", "win32"), patch("builtins.print"):
                    result = live(1, 123, DetectorConfig(), reporter, 0,
                                  music_context_enabled=True)
            finally:
                reporter.close()
            entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(result["music_context_requested"])
        self.assertTrue(poller.stopped)
        audio = next(row for row in entries if row.get("type") == "audio_started")
        self.assertEqual(audio["music_context"]["title"], "Test Song")
        self.assertEqual(
            sum(row.get("type") == "track_observed" for row in entries), 1
        )

    def test_context_start_failure_does_not_interrupt_audio(self):
        class FailingPoller:
            latest = None

            def start(self):
                raise RuntimeError("GSMTC unavailable")

        data = STREAM_HEADER.pack(b"NCM1", RATE, 2, 16) + PACKET_HEADER.pack(0, END_FLAG, 0.0)
        process = LifecycleTests.FakeProcess(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            reporter = Reporter(path, True)
            try:
                with patch("monitor_audio.ensure_helper", return_value=Path("fake.exe")), \
                     patch("monitor_audio.subprocess.Popen", return_value=process), \
                     patch("monitor_audio.MusicContextPoller", return_value=FailingPoller()), \
                     patch("monitor_audio.sys.platform", "win32"), patch("builtins.print"):
                    result = live(1, 123, DetectorConfig(), reporter, 0,
                                  music_context_enabled=True)
            finally:
                reporter.close()
            entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result["audio_seconds"], 0)
        self.assertTrue(result["music_context_requested"])
        error = next(row for row in entries if row.get("type") == "music_context_error")
        self.assertIn("GSMTC unavailable", error["music_context"]["error"])


class LifecycleTests(unittest.TestCase):
    class FakeProcess:
        def __init__(self, data):
            self.stdout = io.BytesIO(data)
            self.stderr = io.BytesIO()
            self.returncode = None
        def poll(self): return self.returncode
        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode
        def terminate(self): self.returncode = -15
        def kill(self): self.returncode = -9

    def _invoke(self, process, reporter, *, popen_error=None):
        with patch("monitor_audio.ensure_helper", return_value=Path("fake.exe")), \
             patch("monitor_audio.subprocess.Popen", return_value=process, side_effect=popen_error), \
             patch("monitor_audio.sys.platform", "win32"), patch("builtins.print"):
            return live(1, 123, DetectorConfig(), reporter, 0)

    def test_popen_failure_preserves_exception(self):
        error = OSError("test process launch failure")
        with tempfile.TemporaryDirectory() as directory:
            reporter = Reporter(Path(directory) / "events.jsonl", True)
            try:
                with patch("monitor_audio.threading.Thread.start") as start:
                    with self.assertRaises(OSError) as caught:
                        self._invoke(None, reporter, popen_error=error)
                self.assertIs(caught.exception, error)
                start.assert_not_called()
            finally:
                reporter.close()

    def test_popen_interrupt_without_child(self):
        with tempfile.TemporaryDirectory() as directory:
            reporter = Reporter(Path(directory) / "events.jsonl", True)
            try:
                with patch("monitor_audio.threading.Thread.start") as start:
                    result = self._invoke(None, reporter, popen_error=KeyboardInterrupt())
                self.assertTrue(result["interrupted"])
                self.assertEqual(result["audio_seconds"], 0)
                start.assert_not_called()
            finally:
                reporter.close()

    def _check_thread_start_failure(self, fail_at, error, *, after_start=False):
        data = STREAM_HEADER.pack(b"NCM1", RATE, 2, 16) + PACKET_HEADER.pack(0, END_FLAG, 0.0)
        process = self.FakeProcess(data)
        real_start = threading.Thread.start
        attempted = []

        def start(thread):
            attempted.append(thread)
            if len(attempted) == fail_at:
                if after_start:
                    real_start(thread)
                raise error
            real_start(thread)

        with tempfile.TemporaryDirectory() as directory:
            reporter = Reporter(Path(directory) / "events.jsonl", True)
            try:
                with patch("monitor_audio.threading.Thread.start", new=start):
                    if isinstance(error, KeyboardInterrupt):
                        result = self._invoke(process, reporter)
                        self.assertTrue(result["interrupted"])
                    else:
                        with self.assertRaises(type(error)) as caught:
                            self._invoke(process, reporter)
                        self.assertIs(caught.exception, error)
                self.assertEqual(len(attempted), fail_at)
                self.assertFalse(any(thread.is_alive() for thread in attempted))
                self.assertEqual(process.returncode, -15)
                self.assertTrue(process.stdout.closed)
                self.assertTrue(process.stderr.closed)
            finally:
                reporter.close()

    def test_first_thread_start_failure_cleans_child(self):
        self._check_thread_start_failure(1, RuntimeError("first thread could not start"))

    def test_second_thread_start_failure_cleans_child(self):
        self._check_thread_start_failure(2, RuntimeError("second thread could not start"))

    def test_thread_start_interrupts_clean_child(self):
        for fail_at in (1, 2):
            for after_start in (False, True):
                with self.subTest(fail_at=fail_at, after_start=after_start):
                    self._check_thread_start_failure(fail_at, KeyboardInterrupt(), after_start=after_start)

    def test_interrupt_terminates_child(self):
        process = self.FakeProcess(STREAM_HEADER.pack(b"NCM1", RATE, 2, 16) + PACKET_HEADER.pack(0, END_FLAG, 0.0))
        with tempfile.TemporaryDirectory() as directory:
            reporter = Reporter(Path(directory) / "events.jsonl", True)
            try:
                with patch.object(reporter, "write", side_effect=KeyboardInterrupt):
                    result = self._invoke(process, reporter)
                self.assertTrue(result["interrupted"])
                self.assertEqual(process.returncode, -15)
                self.assertTrue(process.stdout.closed)
                self.assertTrue(process.stderr.closed)
            finally:
                reporter.close()

    def test_bad_stream_terminates_child(self):
        process = self.FakeProcess(b"bad header")
        with tempfile.TemporaryDirectory() as directory:
            reporter = Reporter(Path(directory) / "events.jsonl", True)
            try:
                with self.assertRaises(RuntimeError):
                    self._invoke(process, reporter)
                self.assertEqual(process.returncode, -15)
                self.assertTrue(process.stdout.closed)
            finally:
                reporter.close()

    def _run_queue_loss(self, queue_type, before_session=None):
        pcm = np.column_stack((tone(0.01), tone(0.01)))
        raw = (pcm * 32767).astype("<i2").tobytes()
        data = STREAM_HEADER.pack(b"NCM1", RATE, 2, 16)
        data += b"".join(PACKET_HEADER.pack(480, 0, (index + 1) * 0.01) + raw for index in range(70))
        data += PACKET_HEADER.pack(0, END_FLAG, 0.71)
        process = self.FakeProcess(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            reporter = Reporter(path, True)
            original_write = reporter.write

            def write(record):
                if record["kind"] == "session" and before_session:
                    before_session()
                original_write(record)

            try:
                with patch("monitor_audio.queue.Queue", queue_type), patch.object(reporter, "write", side_effect=write):
                    result = self._invoke(process, reporter)
            finally:
                reporter.close()
            entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        return result, [row for row in entries if row.get("type") == "capture_gap"]

    def test_missing_initial_packets_reset_at_first_consumed_packet(self):
        filled = threading.Event()

        class InitialOverflowQueue(queue.Queue):
            def put_nowait(self, packet):
                super().put_nowait(packet)
                if packet.sequence == 69:
                    filled.set()

        result, gaps = self._run_queue_loss(
            InitialOverflowQueue, lambda: self.assertTrue(filled.wait(2), "producer did not finish"))
        self.assertEqual(result["queue_dropped_packets"], 6)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["evidence"]["reason"], "packet_sequence_gap")
        self.assertAlmostEqual(gaps[0]["detected_at_s"], 0.06)

    def test_future_queue_drops_do_not_reset_inflight_contiguous_packet(self):
        allow_producer = threading.Event()
        filled = threading.Event()

        class FutureOverflowQueue(queue.Queue):
            def put_nowait(self, packet):
                if packet.sequence == 2 and not allow_producer.wait(2):
                    raise RuntimeError("consumer did not release producer")
                super().put_nowait(packet)
                if packet.sequence == 69:
                    filled.set()

            def get(self, block=True, timeout=None):
                packet = super().get(block=block, timeout=timeout)
                if block and packet.sequence == 1:
                    # Hold sequence 1 in flight while the producer drops 2..5.
                    # It remains contiguous with 0: reset only when 6 is consumed.
                    allow_producer.set()
                    if not filled.wait(2):
                        raise RuntimeError("producer did not complete overflow")
                return packet

        result, gaps = self._run_queue_loss(FutureOverflowQueue)
        self.assertEqual(result["queue_dropped_packets"], 4)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["evidence"]["reason"], "packet_sequence_gap")
        self.assertAlmostEqual(gaps[0]["detected_at_s"], 0.06)
        self.assertAlmostEqual(result["audio_seconds"], 0.66)

    def test_heartbeats_do_not_invent_silence(self):
        data = STREAM_HEADER.pack(b"NCM1", RATE, 2, 16)
        data += b"".join(PACKET_HEADER.pack(0, 0, i / 4) for i in range(1, 5))
        data += PACKET_HEADER.pack(0, END_FLAG, 1.0)
        process = self.FakeProcess(data)
        with tempfile.TemporaryDirectory() as directory:
            reporter = Reporter(Path(directory) / "events.jsonl", True)
            try:
                result = self._invoke(process, reporter)
                self.assertEqual(result["audio_seconds"], 0)
                self.assertEqual(result["events"], {})
            finally:
                reporter.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

