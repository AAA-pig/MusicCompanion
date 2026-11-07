import asyncio
import unittest
from types import SimpleNamespace

from music_companion.music_context import MusicContext, MusicContextPoller, MusicContextReader, _seconds


class FakeSession:
    source_app_user_model_id = "cloudmusic.exe"

    def __init__(self, *, title="Test Song", artist="Test Artist", status="playing", position=12.5, duration=180):
        self._title = title
        self._artist = artist
        self._status = status
        self._position = position
        self._duration = duration

    def get_playback_info(self):
        return SimpleNamespace(playback_status=SimpleNamespace(name=self._status))

    async def try_get_media_properties_async(self):
        return SimpleNamespace(title=self._title, artist=self._artist, album_title="Album")

    def get_timeline_properties(self):
        from datetime import timedelta
        return SimpleNamespace(
            position=timedelta(seconds=self._position) if self._position is not None else None,
            end_time=timedelta(seconds=self._duration) if self._duration is not None else None,
        )


class FakeManager:
    def __init__(self, session):
        self.session = session

    def get_current_session(self):
        return self.session


class MusicContextTests(unittest.TestCase):
    def test_reads_track_status_and_system_timeline(self):
        async def run():
            reader = MusicContextReader(FakeManager(FakeSession()))
            result = await reader.read()
            self.assertTrue(result.available)
            self.assertEqual(result.title, "Test Song")
            self.assertEqual(result.artist, "Test Artist")
            self.assertEqual(result.playback_status, "playing")
            self.assertEqual(result.position_seconds, 12.5)
            self.assertEqual(result.position_source, "gsmTC_timeline")
            self.assertEqual(result.position_confidence, "system_reported")
        asyncio.run(run())

    def test_rejects_non_netease_session(self):
        async def run():
            session = FakeSession()
            session.source_app_user_model_id = "spotify.exe"
            result = await MusicContextReader(FakeManager(session)).read()
            self.assertFalse(result.available)
            self.assertIsNone(result.title)
        asyncio.run(run())

    def test_no_session_is_not_an_error_or_silence(self):
        async def run():
            result = await MusicContextReader(FakeManager(None)).read()
            self.assertFalse(result.available)
            self.assertIsNone(result.error)
            self.assertEqual(result.playback_status, "unknown")
        asyncio.run(run())

    def test_missing_position_is_not_fabricated(self):
        async def run():
            result = await MusicContextReader(FakeManager(FakeSession(position=None))).read()
            self.assertIsNone(result.position_seconds)
            self.assertEqual(result.position_source, "unavailable")
            self.assertEqual(result.position_confidence, "none")
        asyncio.run(run())

    def test_zero_timeline_is_unavailable(self):
        async def run():
            session = FakeSession(position=0, duration=0)
            result = await MusicContextReader(FakeManager(session)).read()
            self.assertIsNone(result.position_seconds)
            self.assertIsNone(result.duration_seconds)
            self.assertEqual(result.position_source, "unavailable")
            self.assertEqual(result.position_confidence, "none")
        asyncio.run(run())

    def test_api_exception_is_returned_as_error_context(self):
        class BrokenManager:
            def get_current_session(self):
                raise RuntimeError("session unavailable")
        async def run():
            result = await MusicContextReader(BrokenManager()).read()
            self.assertFalse(result.available)
            self.assertIn("session unavailable", result.error)
        asyncio.run(run())

    def test_time_span_conversion(self):
        from datetime import timedelta
        self.assertEqual(_seconds(timedelta(seconds=2.5)), 2.5)
        self.assertIsNone(_seconds(None))


class MusicContextPollerTests(unittest.TestCase):
    @staticmethod
    def snapshot(title="Test Song", status="playing", at=1.0, available=True):
        return MusicContext(
            title=title if available else None,
            artist="Test Artist" if available else None,
            playback_status=status if available else "unknown",
            source_app_id="cloudmusic.exe" if available else None,
            sampled_at_monotonic=at,
            available=available,
            error=None if available else "session unavailable",
        )

    def test_initial_observation_dedup_and_playback_change(self):
        poller = MusicContextPoller()
        poller.process_snapshot(self.snapshot())
        poller.process_snapshot(self.snapshot())
        poller.process_snapshot(self.snapshot(status="paused", at=2))
        events = poller.drain_events()
        self.assertEqual(
            [event["type"] for event in events],
            ["track_observed", "playback_changed"],
        )
        self.assertEqual(events[1]["previous_status"], "playing")
        self.assertEqual(events[1]["music_context"]["playback_status"], "paused")

    def test_track_change_requires_two_matching_snapshots(self):
        poller = MusicContextPoller()
        poller.process_snapshot(self.snapshot(title="First"))
        poller.process_snapshot(self.snapshot(title="Second", at=2))
        self.assertEqual(
            [event["type"] for event in poller.drain_events()],
            ["track_observed"],
        )
        poller.process_snapshot(self.snapshot(title="Second", at=3))
        poller.process_snapshot(self.snapshot(title="Second", at=4))
        events = poller.drain_events()
        self.assertEqual([event["type"] for event in events], ["track_changed"])
        self.assertEqual(events[0]["previous_track"]["title"], "First")
        self.assertEqual(events[0]["music_context"]["title"], "Second")

    def test_unavailable_grace_latest_snapshot_and_recovery(self):
        poller = MusicContextPoller(unavailable_after_seconds=3)
        valid = self.snapshot(at=10)
        poller.process_snapshot(valid)
        poller.process_snapshot(self.snapshot(at=11, available=False))
        poller.process_snapshot(self.snapshot(at=12, available=False))
        self.assertIs(poller.latest, valid)
        self.assertEqual(
            [event["type"] for event in poller.drain_events()],
            ["track_observed"],
        )
        poller.process_snapshot(self.snapshot(at=13, available=False))
        self.assertFalse(poller.latest.available)
        poller.process_snapshot(self.snapshot(at=14))
        self.assertEqual(
            [event["type"] for event in poller.drain_events()],
            ["music_context_unavailable", "music_context_recovered"],
        )


if __name__ == "__main__":
    unittest.main()
