import unittest
import tempfile
import json
import threading
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import threads_trainer as app
from threads_trainer import SettingsStore

from threads_trainer import (
    DEFAULT_URL_TEMPLATE,
    collect_post_urls,
    parse_settings,
)


class SettingsTests(unittest.TestCase):
    def test_parse_settings_reads_posts_per_topic(self):
        settings = parse_settings(
            {
                "topics": "Python",
                "urlTemplate": DEFAULT_URL_TEMPLATE,
                "secondsPerTopic": 20,
                "scrollsPerTopic": 3,
                "cooldownSeconds": 1,
                "sessionMinutes": 5,
                "postsPerTopic": 4,
                "headless": False,
            }
        )

        self.assertEqual(settings.posts_per_topic, 4)

    def test_parse_settings_rejects_negative_posts_per_topic(self):
        with self.assertRaisesRegex(ValueError, "貼文"):
            parse_settings(
                {
                    "topics": "Python",
                    "postsPerTopic": -1,
                }
            )


class PostUrlTests(unittest.TestCase):
    def test_collect_post_urls_keeps_unique_threads_post_links(self):
        urls = collect_post_urls(
            [
                "https://www.threads.com/@alice/post/ABC123?x=1",
                "/@bob/post/DEF456",
                "https://www.threads.com/search?q=Python",
                "https://www.example.com/@mallory/post/NOPE",
                "/@alice/post/ABC123?duplicate=1",
            ],
            "https://www.threads.com/search?q=Python",
            limit=5,
        )

        self.assertEqual(
            urls,
            [
                "https://www.threads.com/@alice/post/ABC123",
                "https://www.threads.com/@bob/post/DEF456",
            ],
        )

    def test_collect_post_urls_respects_limit(self):
        urls = collect_post_urls(
            [
                "/@alice/post/ABC123",
                "/@bob/post/DEF456",
                "/@casey/post/GHI789",
            ],
            "https://www.threads.com/search?q=Python",
            limit=2,
        )

        self.assertEqual(len(urls), 2)


class CurrentPageTests(unittest.TestCase):
    def test_only_threads_web_urls_are_accepted(self):
        state = app.AppState()
        self.assertEqual(state.snapshot()["currentUrl"], app.THREADS_HOME)
        for url in ("https://threads.com/search?q=mine", "https://www.threads.net/@user/post/123", "http://threads.net/", "https://www.threads.com/"):
            state.set_current_url(url)
            self.assertEqual(state.snapshot()["currentUrl"], url)
        for url in ("about:blank", "javascript:alert(1)", "https://threads.com.evil.example/", "https://evil.example/?threads.com", "https://user:secret@threads.com/", "https://threads.com@evil.example/", "https://[invalid", None):
            state.set_current_url(url)
            self.assertEqual(state.snapshot()["currentUrl"], "https://www.threads.com/")

    def test_main_frame_navigation_tracks_search_post_and_return(self):
        class Page:
            url = "about:blank"
            main_frame = object()
            def on(self, event, callback):
                self.event = event
                self.callback = callback
            def navigate(self, url):
                self.url = url
                self.callback(self.main_frame)

        page = Page()
        state = app.AppState()
        app.track_page_navigation(page, state.set_current_url)
        self.assertEqual(page.event, "framenavigated")
        search = "https://www.threads.com/search?q=custom"
        for url in (search, "https://www.threads.com/@user/post/123", search):
            page.navigate(url)
            self.assertEqual(state.snapshot()["currentUrl"], url)
        page.url = "https://www.threads.com/iframe"
        page.callback(object())
        self.assertEqual(state.snapshot()["currentUrl"], search)
        page.navigate("https://www.example.com/redirect")
        self.assertEqual(state.snapshot()["currentUrl"], search)
        state.mark_running(False)
        self.assertEqual(state.snapshot()["currentUrl"], search)


class ManagedBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def exercise_handoff(self, fail_headed=False):
        events = []
        contexts = []
        cookies = [{"name": "session", "value": "private", "domain": "localhost", "path": "/"}]
        storage = {"origin": "http://localhost", "values": {"session": "private"}}

        async def launch(**kwargs):
            events.append(("launch", kwargs["headless"], kwargs["user_data_dir"]))
            if fail_headed and not kwargs["headless"]:
                raise RuntimeError("headed unavailable")
            context = MagicMock()
            page = MagicMock()
            page.url = "http://localhost/search?q=custom"
            page.main_frame = object()
            async def evaluate(script, argument=None):
                if "window.scrollX" in script:
                    return {"x": 0, "y": 1300}
                if "Object.entries(sessionStorage)" in script:
                    return storage
            page.evaluate = AsyncMock(side_effect=evaluate)
            page.goto = AsyncMock()
            page.bring_to_front = AsyncMock()
            context.pages = [page]
            context.cookies = AsyncMock(return_value=cookies)
            context.add_cookies = AsyncMock()
            async def close():
                events.append(("close", kwargs["headless"]))
            context.close = AsyncMock(side_effect=close)
            session = MagicMock()
            session.send = AsyncMock(return_value={"identifier": "one-shot", "windowId": 1})
            session.detach = AsyncMock()
            context.new_cdp_session = AsyncMock(return_value=session)
            contexts.append(context)
            return context

        playwright = MagicMock()
        playwright.chromium.launch_persistent_context = AsyncMock(side_effect=launch)
        control = app.BrowserControl()
        states = []
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            browser = app.ManagedBrowser(playwright, profile, True, control,
                                         lambda url: None, lambda *state: states.append(state),
                                         lambda message: None, app.StopSignal())
            await browser.start()
            old_page = browser.page
            control.show_requested.set()
            await browser.show_if_requested()
            self.assertIsNot(browser.page, old_page)
            self.assertEqual(browser.headless, fail_headed)
            self.assertEqual(states[-1][0], "error" if fail_headed else "ready")
            self.assertEqual(events[:3], [("launch", True, str(profile)), ("close", True), ("launch", False, str(profile))])
            contexts[-1].add_cookies.assert_awaited_once_with(cookies)
            contexts[-1].pages[0].goto.assert_awaited_once_with("http://localhost/search?q=custom", wait_until="domcontentloaded", timeout=45000)
            # Actual local-browser QA separately verifies the CDP sessionStorage behavior.
            self.assertEqual(playwright.chromium.launch_persistent_context.await_count, 3 if fail_headed else 2)
            if not fail_headed:
                same_context = browser.context
                control.show_requested.set()
                await browser.show_if_requested()
                self.assertIs(browser.context, same_context)
                self.assertEqual(playwright.chromium.launch_persistent_context.await_count, 2)
            await browser.close()

    async def test_reveal_releases_same_profile_before_launch_and_focus_reuses_context(self):
        await self.exercise_handoff()

    async def test_failed_headed_launch_recovers_same_profile_headless_with_error(self):
        await self.exercise_handoff(fail_headed=True)

    async def test_trainer_login_and_manual_use_the_same_profile_path(self):
        browser = MagicMock()
        for method in ("start", "close", "_focus", "show_if_requested"):
            setattr(browser, method, AsyncMock())
        browser.page.goto = AsyncMock()
        browser.page.is_closed.return_value = True
        runtime = MagicMock()
        runtime.__aenter__ = AsyncMock(return_value=MagicMock())
        runtime.__aexit__ = AsyncMock(return_value=False)
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / "dedicated"
            with patch.object(app, "PROFILE_DIR", profile), patch.object(app, "async_playwright", return_value=runtime), patch.object(app, "ManagedBrowser", return_value=browser) as managed:
                trainer = app.ThreadsTrainer(app.parse_settings({"topics": "custom"}), lambda message: None, app.StopSignal())
                trainer.content = app.ContentStore(Path(directory) / "content.json")
                trainer._cycle_topics = AsyncMock()
                trainer._sample_feed = AsyncMock()
                await trainer.run()
                await app.open_manual_window(app.StopSignal(), app.BrowserControl(), app.THREADS_HOME, login=True)
                await app.open_manual_window(app.StopSignal(), app.BrowserControl(), "https://www.threads.com/@user/post/current")
                self.assertEqual(managed.call_count, 3)
                self.assertEqual([call.args[1] for call in managed.call_args_list], [profile, profile, profile])

    async def test_interruptible_sleep_services_show_requests_without_changing_settings(self):
        settings = app.parse_settings({"topics": "custom", "headless": True})
        trainer = app.ThreadsTrainer(settings, lambda message: None, app.StopSignal())
        trainer.browser = MagicMock()
        trainer.browser.show_if_requested = AsyncMock()
        with patch.object(app.asyncio, "sleep", new=AsyncMock()):
            await trainer._sleep_interruptibly(1)
        self.assertEqual(trainer.browser.show_if_requested.await_count, 10)
        self.assertTrue(trainer.settings.headless)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "settings.json"
        self.store = SettingsStore(self.path)

    def test_roundtrip_and_checkpoint_isolation(self):
        run = {"topics": "my custom topic", "language": "en", "headless": True}
        self.store.save(run, checkpoint=True)
        self.store.save({"topics": "new draft", "sessionMinutes": ""})
        loaded = SettingsStore(self.path).snapshot()
        self.assertEqual(loaded["lastRun"], run)
        self.assertEqual(loaded["draft"]["topics"], "new draft")
        self.assertNotEqual(loaded["draftSavedAt"], loaded["lastRunAt"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_empty_draft_and_ui_are_independent(self):
        self.store.save(ui={"topicsCollapsed": True, "logCollapsed": True}, draft_present=False)
        self.assertIsNone(self.store.snapshot()["draft"])
        self.store.save({"topics": ""})
        loaded = SettingsStore(self.path).snapshot()
        self.assertEqual(loaded["draft"], {"topics": ""})
        self.assertTrue(loaded["ui"]["topicsCollapsed"])
        self.assertIsNone(loaded["lastRun"])

    def test_corrupt_file_is_not_overwritten(self):
        self.path.write_text("broken")
        store = SettingsStore(self.path)
        self.assertTrue(store.snapshot()["error"])
        with self.assertRaises(OSError):
            store.save({"topics": ""})
        self.assertEqual(self.path.read_text(), "broken")
        self.path.unlink()
        self.assertIsNone(store.snapshot(retry=True)["error"])

    def test_atomic_replace_failure_preserves_file_and_memory(self):
        self.store.save({"topics": "old"})
        before = self.path.read_bytes()
        with patch.object(app.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.store.save({"topics": "new"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.snapshot()["draft"]["topics"], "old")
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.path])

    def test_save_sequence_rejects_stale_requests_and_retries_failed_sequence(self):
        self.store.save({"topics": "latest"}, client_id="page-a", sequence=2)
        self.store.save({"topics": "stale"}, client_id="page-a", sequence=1)
        self.assertEqual(self.store.snapshot()["draft"]["topics"], "latest")
        with patch.object(app.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.store.save({"topics": "retry"}, client_id="page-a", sequence=3)
        self.store.save({"topics": "retry"}, client_id="page-a", sequence=3)
        self.assertEqual(self.store.snapshot()["draft"]["topics"], "retry")
        self.store.save({"topics": "independent"}, client_id="page-b", sequence=1)
        self.assertEqual(self.store.snapshot()["draft"]["topics"], "independent")

    def test_save_sequence_validation_and_bounded_tracking(self):
        for client, sequence in (("bad client", 1), ("page", True), ("page", 0), (None, 1)):
            with self.assertRaises(ValueError):
                self.store.save({"topics": ""}, client_id=client, sequence=sequence)
        for index in range(130):
            self.store.save({"topics": ""}, client_id=f"page-{index}", sequence=1)
        self.assertEqual(len(self.store.client_sequences), 128)

    def test_load_sanitizes_unknown_fields_and_supports_old_version_one(self):
        self.path.write_text(json.dumps({"version": 1, "draft": {"topics": "mine", "category": "old"}, "lastRun": None}))
        loaded = SettingsStore(self.path).snapshot()
        self.assertEqual(loaded["draft"], {"topics": "mine"})
        self.assertEqual(loaded["ui"], {"topicsCollapsed": False, "logCollapsed": False, "reportsCollapsed": False})


class HttpTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = SettingsStore(Path(directory.name) / "settings.json")
        self.state = app.AppState()
        for name, value in (("STORE", self.store), ("STATE", self.state), ("run_worker", lambda *args: None), ("run_login_worker", lambda *args: None), ("run_manual_worker", lambda *args: None)):
            patcher = patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.RequestHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, route, data=None):
        request = Request(self.url + route, data=None if data is None else json.dumps(data).encode(), headers={"Content-Type": "application/json"})
        try:
            response = urlopen(request)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_start_checkpoints_only_valid_accepted_run_and_rejects_busy(self):
        self.store.save({"topics": "checkpoint"}, checkpoint=True)
        for payload in ({"topics": ""}, {"topics": "custom", "secondsPerTopic": 10.5}, {"topics": "custom", "sessionMinutes": 241}):
            self.assertEqual(self.request("/api/start", payload)[0], 400)
            self.assertEqual(self.store.snapshot()["lastRun"]["topics"], "checkpoint")
        self.state.set_current_url("https://www.threads.com/old")
        payload = {"topics": "custom\nsecond", "language": "en", "secondsPerTopic": "35", "headless": True}
        self.assertEqual(self.request("/api/start", payload)[0], 200)
        self.assertEqual(self.state.snapshot()["currentUrl"], app.THREADS_HOME)
        self.assertEqual(self.store.snapshot()["lastRun"], payload)
        self.assertEqual(self.request("/api/start", {"topics": "other"})[0], 409)
        self.assertEqual(self.request("/api/state")[1]["mode"], "browsing")

    def test_failed_persistence_releases_running_claim_and_preserves_checkpoint(self):
        self.store.save({"topics": "checkpoint"}, checkpoint=True)
        with patch.object(app.os, "replace", side_effect=OSError("disk unavailable")):
            self.assertEqual(self.request("/api/start", {"topics": "new"})[0], 500)
        self.assertFalse(self.state.snapshot()["running"])
        self.assertEqual(self.state.snapshot()["mode"], "idle")
        self.assertEqual(self.store.snapshot()["lastRun"]["topics"], "checkpoint")

    def test_active_browser_request_reuses_worker_and_control(self):
        self.request("/api/start", {"topics": "custom", "headless": True})
        worker = self.state.worker_thread
        control = self.state.browser_control
        self.assertEqual(self.request("/api/browser", {})[0], 200)
        self.assertIs(self.state.worker_thread, worker)
        self.assertIs(self.state.browser_control, control)
        self.assertTrue(control.show_requested.is_set())
        self.assertEqual(self.state.snapshot()["browser"]["status"], "opening")
        self.assertTrue(self.state.snapshot()["browser"]["headless"])

    def test_idle_browser_claims_manual_mode_and_excludes_other_workers(self):
        self.state.set_current_url("https://www.threads.com/@user/post/last")
        self.assertEqual(self.request("/api/browser", {})[0], 200)
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["mode"], "browser")
        self.assertEqual(snapshot["browser"]["status"], "starting")
        self.assertEqual(snapshot["currentUrl"], "https://www.threads.com/@user/post/last")
        self.assertEqual(self.request("/api/login", {})[0], 409)
        self.assertEqual(self.request("/api/start", {"topics": "custom"})[0], 409)
        self.assertEqual(self.request("/api/stop", {})[0], 200)
        self.assertTrue(self.state.stop_signal.is_set())

    def test_draft_ui_and_login_do_not_replace_checkpoint(self):
        self.store.save({"topics": "checkpoint"}, checkpoint=True)
        self.assertEqual(self.request("/api/settings", {"draft": {"topics": ""}})[0], 200)
        self.assertEqual(self.request("/api/settings", {"ui": {"topicsCollapsed": True, "logCollapsed": False}})[0], 200)
        self.assertEqual(self.request("/api/login", {})[0], 200)
        self.assertEqual(self.request("/api/state")[1]["mode"], "login")
        self.assertEqual(self.request("/api/settings")[1]["lastRun"]["topics"], "checkpoint")
        self.assertEqual(self.store.snapshot()["draft"], {"topics": ""})


if __name__ == "__main__":
    unittest.main()
