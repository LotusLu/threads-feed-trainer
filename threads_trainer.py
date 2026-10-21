from __future__ import annotations

import asyncio
import json
import copy
import os
import tempfile
from datetime import datetime, timezone
import random
import socket
import threading
import time
import webbrowser
import uuid
from dataclasses import dataclass
from trainer_content import (ContentStore, canonical_post, classify, keywords, fingerprint, compare_reports, EXTRACT_POSTS_JS)
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover - surfaced in the web UI at runtime
    PlaywrightTimeoutError = None
    async_playwright = None


APP_TITLE = "Threads Feed Trainer"
THREADS_HOME = "https://www.threads.com/"
THREADS_HOSTS = {"threads.com", "www.threads.com", "threads.net", "www.threads.net"}
DEFAULT_URL_TEMPLATE = "https://www.threads.com/search?q={query}"
PROFILE_DIR = Path(__file__).resolve().parent / ".browser-profile"
POST_PATH_MARKER = "/post/"
VISIBLE_TEXT_LIMIT = 180
SETTINGS_PATH = Path(__file__).resolve().parent / ".trainer-settings.json"
NUMERIC_FIELDS = {"secondsPerTopic": (10, 600), "scrollsPerTopic": (1, 80),
                  "postsPerTopic": (0, 20), "cooldownSeconds": (0, 120),
                  "sessionMinutes": (1, 240), "feedSampleSize": (1, 50)}


def sanitize_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("設定必須是物件。")
    result: dict[str, object] = {}
    for key, item in value.items():
        if key in {"topics", "urlTemplate", "includeKeywords", "excludeKeywords"}:
            if not isinstance(item, str) or len(item) > (50000 if key in {"topics", "includeKeywords", "excludeKeywords"} else 2048):
                raise ValueError("設定文字過長或格式錯誤。")
            result[key] = item
        elif key in NUMERIC_FIELDS:
            if isinstance(item, bool) or not isinstance(item, (str, int)):
                raise ValueError("數字設定格式錯誤。")
            if isinstance(item, str):
                if len(item) > 16:
                    raise ValueError("數字設定過長。")
            elif abs(item) > 1000000:
                raise ValueError("數字設定過大。")
            result[key] = item
        elif key in {"headless", "feedCheck"}:
            if not isinstance(item, bool):
                raise ValueError("背景模式格式錯誤。")
            result[key] = item
        elif key == "language":
            if item not in {"zh", "en"}:
                raise ValueError("語言格式錯誤。")
            result[key] = item
    return result


class SettingsStore:
    """Server-owned drafts and execution checkpoints; failed loads never get overwritten."""
    def __init__(self, path: Path = SETTINGS_PATH) -> None:
        self.path = Path(path)
        self.lock = threading.Lock()
        self.data = {"version": 1, "draft": None, "lastRun": None,
                     "draftSavedAt": None, "lastRunAt": None,
                     "ui": {"topicsCollapsed": False, "logCollapsed": False, "reportsCollapsed": False}}
        self.client_sequences: dict[str, int] = {}
        self.error: str | None = None
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or value.get("version") != 1:
                    raise ValueError("不支援的設定檔格式。")
                for key in ("draft", "lastRun"):
                    if value.get(key) is not None:
                        value[key] = sanitize_snapshot(value[key])
                for key in ("draftSavedAt", "lastRunAt"):
                    if value.get(key) is not None and not isinstance(value[key], str):
                        raise ValueError("設定時間格式錯誤。")
                value["ui"] = self._sanitize_ui(value.get("ui", {}))
                self.data = {key: value.get(key) for key in self.data}
            self.error = None
        except (OSError, ValueError, TypeError) as exc:
            self.error = f"無法讀取本機設定檔：{exc}"

    def snapshot(self, retry: bool = False) -> dict[str, object]:
        with self.lock:
            if retry and self.error:
                self._load()
            return {**copy.deepcopy(self.data), "error": self.error}

    @staticmethod
    def _sanitize_ui(value: object) -> dict[str, bool]:
        if not isinstance(value, dict) or any(not isinstance(item, bool) for item in value.values()):
            raise ValueError("顯示設定格式錯誤。")
        result = {key: value.get(key, False) for key in ("topicsCollapsed", "logCollapsed")}
        result["reportsCollapsed"] = value.get("reportsCollapsed", result["topicsCollapsed"] and result["logCollapsed"])
        return result

    def save(self, draft: object = None, *, checkpoint: bool = False, ui: object = None, draft_present: bool = True, client_id: object = None, sequence: object = None) -> dict[str, object]:
        if client_id is not None or sequence is not None:
            if (not isinstance(client_id, str) or not 1 <= len(client_id) <= 80
                    or not all(char.isascii() and (char.isalnum() or char in "-_") for char in client_id)
                    or isinstance(sequence, bool) or not isinstance(sequence, int)
                    or not 1 <= sequence <= 9007199254740991):
                raise ValueError("儲存序號格式錯誤。")
        clean = sanitize_snapshot(draft) if draft_present else None
        clean_ui = self._sanitize_ui(ui) if ui is not None else None
        with self.lock:
            if self.error:
                raise OSError(self.error)
            if client_id is not None and sequence <= self.client_sequences.get(client_id, 0):
                return {**copy.deepcopy(self.data), "error": None}
            updated = copy.deepcopy(self.data)
            now = datetime.now(timezone.utc).isoformat()
            if draft_present:
                updated.update(draft=clean, draftSavedAt=now)
            if clean_ui is not None:
                updated["ui"] = clean_ui
            if checkpoint:
                updated.update(lastRun=copy.deepcopy(clean), lastRunAt=now)
            temporary = None
            try:
                fd, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    os.fchmod(output.fileno(), 0o600)
                    json.dump(updated, output, ensure_ascii=False)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, self.path)
                temporary = None
                self.data = updated
                if client_id is not None:
                    self.client_sequences.pop(client_id, None)
                    self.client_sequences[client_id] = sequence
                    if len(self.client_sequences) > 128:
                        self.client_sequences.pop(next(iter(self.client_sequences)))
            finally:
                if temporary:
                    os.unlink(temporary)
            return {**copy.deepcopy(self.data), "error": None}


STORE = SettingsStore()
CONTENT = ContentStore(Path(__file__).with_name(".trainer-content.json"))


@dataclass(frozen=True)
class TrainerSettings:
    topics: list[str]
    url_template: str
    seconds_per_topic: int
    scrolls_per_topic: int
    posts_per_topic: int
    cooldown_seconds: int
    session_minutes: int
    headless: bool
    include_keywords: str = ""
    exclude_keywords: str = ""
    feed_check: bool = True
    feed_sample_size: int = 20


class StopSignal:
    def __init__(self) -> None:
        self._event = threading.Event()

    def stop(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


class BrowserControl:
    def __init__(self) -> None:
        self.show_requested = threading.Event()


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.logs: list[dict[str, str]] = []
        self.worker_thread: threading.Thread | None = None
        self.stop_signal: StopSignal | None = None
        self.running = False
        self.browse_stopped = False
        self.mode = "idle"
        self.current_url = THREADS_HOME
        self.browser_control: BrowserControl | None = None
        self.browser = {"status": "closed", "headless": False, "error": None}

    def add_log(self, message: str) -> None:
        with self.lock:
            self.logs.append({"time": time.strftime("%H:%M:%S"), "message": message})
            self.logs = self.logs[-300:]

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {"running": self.running, "mode": self.mode, "currentUrl": self.current_url, "browser": dict(self.browser), "stopRequested": bool(self.browse_stopped or (self.mode == "browsing" and self.stop_signal and self.stop_signal.is_set())), "logs": list(self.logs), "stats": dict(getattr(self, "stats", {})), **CONTENT.snapshot()}

    def set_current_url(self, url: str) -> None:
        try:
            parsed = urlparse(url)
            valid = (isinstance(url, str) and len(url) <= 8192
                     and parsed.scheme in {"http", "https"}
                     and parsed.hostname in THREADS_HOSTS
                     and parsed.username is None and parsed.password is None)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if valid:
            with self.lock:
                self.current_url = url

    def set_browser_state(self, status: str, headless: bool, error: str | None = None) -> None:
        with self.lock:
            self.browser = {"status": status, "headless": headless, "error": error}

    def mark_running(self, running: bool) -> None:
        with self.lock:
            self.running = running
            if not running:
                if self.mode == "browsing":
                    self.browse_stopped = bool(self.stop_signal and self.stop_signal.is_set())
                self.mode = "idle"


def collect_post_urls(raw_hrefs: list[str], base_url: str, limit: int) -> list[str]:
    urls = []
    seen = set()
    if limit <= 0:
        return urls
    for href in raw_hrefs:
        post = canonical_post(href, base_url)
        if post and post[0] not in seen:
            seen.add(post[0])
            urls.append(post[1])
            if len(urls) >= limit:
                break
    return urls


def summarize_visible_text(text: str, limit: int = VISIBLE_TEXT_LIMIT) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit].rstrip()}..."


def track_page_navigation(page, on_page_change: Callable[[str], None]) -> None:
    """Observe main-frame commits, including redirects and navigation timeouts."""
    def navigated(frame) -> None:
        if frame == page.main_frame:
            on_page_change(page.url)

    page.on("framenavigated", navigated)
    on_page_change(page.url)


class ManagedBrowser:
    """Only the owning asyncio worker touches this profile or its Playwright objects."""
    def __init__(self, playwright, profile: Path, headless: bool, control: BrowserControl,
                 on_page_change: Callable[[str], None],
                 on_state: Callable[[str, bool, str | None], None],
                 log: Callable[[str], None], stop_signal: StopSignal) -> None:
        self.playwright = playwright
        self.profile = profile
        self.headless = headless
        self.control = control
        self.on_page_change = on_page_change
        self.on_state = on_state
        self.log = log
        self.stop_signal = stop_signal
        self.context = None
        self.page = None
        self.last_error: str | None = None

    def _report(self, status: str, error: str | None = None) -> None:
        self.last_error = error
        if status == "ready" and self.control.show_requested.is_set():
            status = "opening"
        self.on_state(status, self.headless, error)

    async def _launch(self, headless: bool) -> None:
        self.profile.mkdir(parents=True, exist_ok=True)
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile), headless=headless,
            viewport={"width": 1280, "height": 900}, locale="zh-TW",
            args=["--disable-blink-features=AutomationControlled", "--no-default-browser-check"],
        )
        self.headless = headless
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        track_page_navigation(self.page, self.on_page_change)

    async def start(self) -> None:
        self._report("starting")
        try:
            await self._launch(self.headless)
            self._report("ready")
        except Exception as exc:
            self._report("error", str(exc))
            raise

    async def _close_context(self) -> None:
        if self.context is not None:
            await self.context.close()
            self.context = None
            self.page = None

    async def close(self) -> None:
        try:
            await self._close_context()
        finally:
            self._report("error" if self.last_error else "closed", self.last_error)

    async def _focus(self) -> None:
        await self.page.bring_to_front()
        session = None
        try:
            session = await self.context.new_cdp_session(self.page)
            window = await session.send("Browser.getWindowForTarget")
            await session.send("Browser.setWindowBounds", {
                "windowId": window["windowId"], "bounds": {"windowState": "normal"},
            })
        except Exception:
            pass  # Window restoration is platform-specific; bring_to_front still applies.
        finally:
            if session is not None:
                try:
                    await session.detach()
                except Exception:
                    pass

    async def _restore(self, url: str, scroll: dict[str, float], cookies: list[dict], session_storage: dict | None) -> None:
        # Handoff data stays in this worker's memory and this exact dedicated profile.
        await self.context.add_cookies(cookies)
        session = None
        identifier = None
        try:
            if session_storage is not None:
                session = await self.context.new_cdp_session(self.page)
                await session.send("Page.enable")
                script = """(() => { const saved = %s;
                    if (location.origin === saved.origin) {
                        for (const [key, value] of Object.entries(saved.values)) sessionStorage.setItem(key, value);
                    }
                })();""" % json.dumps(session_storage)
                result = await session.send("Page.addScriptToEvaluateOnNewDocument", {"source": script})
                identifier = result["identifier"]
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except PlaywrightTimeoutError:
                self.log("顯示瀏覽器：恢復頁面載入逾時。")
        finally:
            if session is not None:
                try:
                    if identifier is not None:
                        await session.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": identifier})
                finally:
                    await session.detach()
        await self.page.evaluate("position => window.scrollTo(position.x, position.y)", scroll)

    async def show_if_requested(self) -> None:
        if not self.control.show_requested.is_set() or self.stop_signal.is_set():
            return
        self.control.show_requested.clear()
        self._report("opening")
        if not self.headless:
            try:
                await self._focus()
                self._report("ready")
            except Exception as exc:
                self._report("error", str(exc))
                self.log(f"無法顯示專用瀏覽器：{exc}")
            return

        url = self.page.url if self.page.url != "about:blank" else THREADS_HOME
        try:
            scroll = await self.page.evaluate("() => ({x: window.scrollX, y: window.scrollY})")
        except Exception:
            scroll = {"x": 0, "y": 0}
        try:
            cookies = await self.context.cookies()
            session_storage = await self.page.evaluate("""() => location.origin === 'null' ? null :
                ({origin: location.origin, values: Object.fromEntries(Object.entries(sessionStorage))})""")
        except Exception as exc:
            self._report("error", str(exc))
            self.log(f"無法安全切換專用瀏覽器，背景瀏覽繼續：{exc}")
            return
        # Release the persistent profile before launching its headed replacement.
        await self._close_context()
        try:
            await self._launch(False)
            await self._restore(url, scroll, cookies, session_storage)
            await self._focus()
            self._report("ready")
            self.log("已顯示同一個專用瀏覽器，繼續目前的瀏覽進度。")
        except Exception as exc:
            error = str(exc)
            await self._close_context()
            try:
                await self._launch(True)
                await self._restore(url, scroll, cookies, session_storage)
            except Exception as recovery_error:
                self._report("error", f"{error}; 恢復背景瀏覽失敗：{recovery_error}")
                raise
            self._report("error", error)
            self.log(f"無法顯示專用瀏覽器，已恢復背景瀏覽，可重試：{error}")


class ThreadsTrainer:
    def __init__(
        self,
        settings: TrainerSettings,
        log: Callable[[str], None],
        stop_signal: StopSignal,
        on_page_change: Callable[[str], None] | None = None,
        control: BrowserControl | None = None,
        on_browser_state: Callable[[str, bool, str | None], None] | None = None,
    ) -> None:
        self.settings = settings
        self.log = log
        self.stop_signal = stop_signal
        self.on_page_change = on_page_change or (lambda url: None)
        self.control = control or BrowserControl()
        self.on_browser_state = on_browser_state or (lambda status, headless, error: None)
        self.browser: ManagedBrowser | None = None
        self.content = CONTENT
        self.deadline = float("inf")
        self.stats = {key: 0 for key in ("candidates", "matched", "opened", "read", "duplicates", "excluded", "unknown", "errors")}
        self.considered: set[tuple[str, str]] = set()
        self.read_ids: set[str] = set()
        self.run_id = uuid.uuid4().hex
        self.before = None
        self.history_warning = False
        self.final_ids: set[str] = set()

    def active(self, deadline: float | None = None) -> bool:
        return not self.stop_signal.is_set() and time.monotonic() < min(self.deadline, deadline if deadline is not None else float("inf"))

    def publish(self) -> None:
        with STATE.lock:
            STATE.stats = dict(self.stats)

    def rules(self, topic: str | None = None) -> tuple[list[str], list[str]]:
        return (keywords(self.settings.include_keywords) or keywords(topic if topic is not None else "\n".join(self.settings.topics)), keywords(self.settings.exclude_keywords))

    def timeout(self, deadline: float) -> int:
        return max(1, min(8000, int((min(deadline, self.deadline) - time.monotonic()) * 1000)))

    async def navigate(self, browser, url: str, deadline: float) -> bool:
        if not self.active(deadline):
            return False
        await browser.show_if_requested()
        if not self.active(deadline):
            return False
        try:
            await browser.page.goto(url, wait_until="domcontentloaded", timeout=self.timeout(deadline))
            return self.active(deadline)
        except Exception as exc:
            self.stats["errors"] += 1
            self.log("頁面載入未完成：可能逾時或頁面已變更，繼續下一個可用步驟。")
            return False

    async def cards(self, browser, deadline: float, wait: bool = False) -> list[dict]:
        until = min(deadline, self.deadline, time.monotonic() + (4 if wait else 0.1))
        result = []
        while self.active(until):
            await browser.show_if_requested()
            try:
                raw = await browser.page.evaluate(EXTRACT_POSTS_JS)
                result = []
                for card in raw:
                    post = canonical_post(card.get("url"))
                    if post:
                        result.append({**card, "id": post[0], "url": post[1]})
                if any(card.get("text", "").strip() for card in result) or not wait:
                    return result
            except Exception:
                self.stats["errors"] += 1
                return []
            await self.pause(0.2, until)
        return result

    async def pause(self, seconds: float, deadline: float) -> bool:
        target = time.monotonic() + seconds
        for _ in range(max(1, int(seconds * 10 + .999))):
            if not self.active(deadline) or time.monotonic() >= target:
                break
            if self.browser is not None:
                await self.browser.show_if_requested()
            await asyncio.sleep(min(0.1, max(0, target-time.monotonic()), max(0, min(deadline,self.deadline)-time.monotonic())))
        return self.active(deadline) and time.monotonic() >= target


    async def run(self) -> None:
        if async_playwright is None:
            raise RuntimeError(
                "缺少 Playwright。請先執行：pip install -r requirements.txt && "
                "python -m playwright install chromium"
            )

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.settings.session_minutes * 60
        self.deadline = deadline
        self.publish()
        try:
            self.content.start_run()
        except OSError as exc:
            self.log(str(exc))
        if self.content.error:
            self.log(self.content.error)

        async with async_playwright() as playwright:
            self.browser = ManagedBrowser(playwright, PROFILE_DIR, self.settings.headless,
                                          self.control, self.on_page_change, self.on_browser_state,
                                          self.log, self.stop_signal)
            try:
                await self.browser.start()
                # Both samples are inside the wall-clock budget. Reserve at most 15 s
                # for the final sample; baseline is capped at 15 s too.
                reserve = min(15, self.settings.session_minutes * 60 / 5) if self.settings.feed_check else 0
                if self.settings.feed_check and self.active():
                    self.before = await self._sample_feed(self.browser, "before", min(deadline-reserve, time.monotonic()+15))
                await self._cycle_topics(self.browser, deadline-reserve)
                if self.settings.feed_check and self.active():
                    await self._sample_feed(self.browser, "after", deadline)
                self.publish()
            finally:
                await self.browser.close()

    async def _cycle_topics(self, browser, deadline: float) -> None:
        self.log("以本機關鍵字規則選讀；不會自動按讚、追蹤或送出回饋。")
        while self.active(deadline):
            topics = self.settings.topics[:]
            random.shuffle(topics)
            for topic in topics:
                if not self.active(deadline):
                    break
                topic_deadline = min(deadline, time.monotonic() + self.settings.seconds_per_topic)
                self.log(f"開啟主題：{topic}")
                if await self.navigate(browser, self._topic_url(topic), topic_deadline):
                    await self._browse_topic(browser, topic, topic_deadline)
                await self.pause(self.settings.cooldown_seconds, deadline)
        self.log("主題瀏覽已結束。")

    def _topic_url(self, topic: str) -> str:
        template = self.settings.url_template.strip() or DEFAULT_URL_TEMPLATE
        if "{query}" not in template:
            template = DEFAULT_URL_TEMPLATE
        return template.format(query=quote_plus(topic))

    async def _browse_topic(self, browser, topic: str, deadline: float | None = None) -> None:
        deadline = min(self.deadline, deadline or time.monotonic()+self.settings.seconds_per_topic)
        opened = 0
        self.current_topic = topic
        started_stats = dict(self.stats)
        previewed_ids: set[str] = set()
        includes, excludes = self.rules(topic)
        for index in range(self.settings.scrolls_per_topic):
            if not self.active(deadline):
                break
            opened_on_view = False
            representative = None
            for card in await self.cards(browser, deadline, wait=True):
                if not self.active(deadline):
                    break
                post_id = card['id']
                entry = self.content.entry(post_id)
                can_preview = post_id not in previewed_ids and bool(card.get('text', '').strip())
                if can_preview and representative is None:
                    representative = (card, '檢視搜尋結果')
                if entry or post_id in self.read_ids:
                    if can_preview and representative and representative[0]['id'] == post_id:
                        representative = (card, '檢視搜尋結果／已讀略過')
                    self.stats['duplicates'] += 1
                    continue
                if (topic, post_id) in self.considered:
                    continue
                self.stats['candidates'] += 1
                result = classify(card.get('text',''), includes, excludes, entry.get('feedback'))
                if card.get('sponsored'):
                    result = 'excluded'
                if result in {'excluded', 'unknown'}:
                    self.stats[result] += 1
                if result != 'unknown':
                    self.considered.add((topic, post_id))
                if result != 'matched':
                    if can_preview and representative and representative[0]['id'] == post_id:
                        representative = (card, '檢視搜尋結果／未符合主題')
                    continue
                self.stats['matched'] += 1
                if opened >= self.settings.posts_per_topic:
                    continue
                opened_on_view = True
                if await self._read_card(browser, card, includes, excludes, deadline):
                    self.stats['read'] += 1
                opened += 1
                self.publish()
            if not opened_on_view and representative:
                card, label = representative
                self._post_activity(card, label)
                previewed_ids.add(card['id'])
            if not self.active(deadline):
                break
            self.log(f"{summarize_visible_text(topic, 60)}：檢查第 {index + 1}/{self.settings.scrolls_per_topic} 個畫面，接著向下瀏覽。")
            await browser.show_if_requested()
            await browser.page.mouse.wheel(0, 650)
            await self.pause(1.5, deadline)
        self.log(f"{summarize_visible_text(topic, 60)}：本次主題完成閱讀 {self.stats['read'] - started_stats['read']} 篇，符合 {self.stats['matched'] - started_stats['matched']} 篇，略過已讀 {self.stats['duplicates'] - started_stats['duplicates']} 次。")
        self.publish()

    async def _wait_for_post_detail(self, browser, post_id: str, deadline: float,
                                    timeout: float = 4.0) -> dict | None:
        """SPA clicks may leave hydrated search cards visible before URL commit."""
        until = min(self.deadline, deadline, time.monotonic() + timeout)
        while self.active(until):
            await browser.show_if_requested()
            if not self.active(until):
                break
            current = canonical_post(browser.page.url)
            if current and current[0] == post_id:
                cards = await self.cards(browser, until, wait=False)
                # A reveal can replace the page; check the current owner again.
                current = canonical_post(browser.page.url)
                if current and current[0] == post_id:
                    detail = next((item for item in cards if item['id'] == post_id and item.get('text', '').strip()), None)
                    if detail:
                        return detail
            await self.pause(.1, until)
        return None

    def _post_activity(self, card: dict, message: str, text: str | None = None) -> None:
        """Only a bounded own-post preview is logged; never whole-page/account text."""
        canonical = canonical_post(card.get('url', ''))
        url = canonical[1] if canonical else '（貼文網址無法確認）'
        topic = summarize_visible_text(getattr(self, 'current_topic', '目前主題'), 60)
        preview = summarize_visible_text(text if text is not None else card.get('text', ''), 180)
        self.log(f"{topic}：{message} · {url}" + (f" · {preview}" if preview else ''))

    async def _read_card(self, browser, card, includes, excludes, deadline) -> bool:
        def skip(reason: str) -> bool:
            self._post_activity(card, f"略過：{reason}")
            # Stop/budget expiry is an expected limit, not an operation error.
            if reason not in {'已收到停止要求', '本主題或總時間已用完', '正文不再符合包含／排除關鍵字規則'}:
                self.stats['errors'] += 1
            return False

        def limit_reason() -> str:
            return '已收到停止要求' if self.stop_signal.is_set() else '本主題或總時間已用完'

        if not self.active(deadline):
            return skip(limit_reason())
        self._post_activity(card, '準備開啟貼文')
        read = False
        search_url = None
        scroll = None
        stage = '開啟貼文'
        try:
            await browser.show_if_requested()
            search_url = browser.page.url
            scroll = await browser.page.evaluate("() => ({x:scrollX,y:scrollY})")
            # Click only an actual rendered permalink from the current card.
            locator = browser.page.locator('a[href]')
            clicked = False
            for i in range(await locator.count()):
                if not self.active(deadline):
                    return skip(limit_reason())
                anchor = locator.nth(i)
                href = await anchor.get_attribute('href')
                post = canonical_post(href or '', browser.page.url)
                if post and post[0] == card['id'] and await anchor.is_visible():
                    await anchor.click(timeout=self.timeout(deadline))
                    clicked = True
                    break
            if not clicked:
                return skip('畫面上的貼文連結已消失或無法點擊')
            self.stats['opened'] += 1
            self._post_activity(card, '已開啟貼文，等待正文')
            stage = '等待貼文正文'
            detail = await self._wait_for_post_detail(browser, card['id'], deadline)
            if not self.active(deadline):
                return skip(limit_reason())
            current = canonical_post(browser.page.url)
            if not current or current[0] != card['id']:
                return skip('開啟後的網址不是這篇貼文')
            if not detail:
                return skip('正文尚未載入，或無法確認這篇貼文的內容')
            entry = self.content.entry(card['id'])
            if classify(detail['text'], includes, excludes, entry.get('feedback')) != 'matched':
                self.stats['excluded'] += 1
                return skip('正文不再符合包含／排除關鍵字規則')
            dwell = min(14, max(4, len(detail['text']) / 35))
            self._post_activity(card, f"正文已就緒，開始閱讀，預計 {dwell:.1f} 秒", detail['text'])
            stage = '閱讀貼文'
            if await self.pause(dwell, deadline):
                try:
                    self.content.read(card['url'])
                except OSError:
                    if not self.history_warning:
                        self.log('本機閱讀紀錄暫時無法寫入；本次仍會避開已讀貼文。請查看瀏覽成果的統計與比較方式。')
                        self.history_warning = True
                self.read_ids.add(card['id'])
                read = True
                self._post_activity(card, '完成閱讀', detail['text'])
            else:
                return skip(limit_reason())
        except Exception:
            return skip(f'{stage}未完成，可能逾時或頁面內容已變更')
        finally:
            if search_url and scroll is not None and self.active(deadline):
                try:
                    if await self.navigate(browser, search_url, deadline):
                        await browser.page.evaluate("p => scrollTo(p.x,p.y)", scroll)
                except Exception:
                    self.stats['errors'] += 1
                    self.log('返回搜尋頁時未能恢復捲動位置，將繼續檢查目前畫面。')
        return read

    async def _verify_for_you(self, browser, deadline: float) -> bool:
        # A homepage URL alone does not establish feed identity or login.
        names = {"For you", "For You", "為你推薦", "为你推荐"}
        until = min(deadline, time.monotonic()+4)
        while self.active(until):
            await browser.show_if_requested()
            for role in ('tab', 'button', 'link'):
                for name in names:
                    tabs = browser.page.get_by_role(role, name=name, exact=True)
                    for i in range(await tabs.count()):
                        tab = tabs.nth(i)
                        if not await tab.is_visible():
                            continue
                        if await tab.get_attribute('aria-selected') == 'true' or await tab.get_attribute('aria-current') in {'page','true'}:
                            # Reject explicit login gates even if navigation is present.
                            import re
                            for gate_role in ('button', 'link'):
                                gates = browser.page.get_by_role(gate_role, name=re.compile(r'^(Log in|Log in to Threads|Sign in|登入|登录|登入 Threads)$', re.I))
                                for gate_index in range(await gates.count()):
                                    if await gates.nth(gate_index).is_visible():
                                        return False
                            # Require affirmative account navigation, without opening it.
                            # Public profile links / arbitrary author names are insufficient.
                            profile_names = ('Profile', '個人檔案', '個人主頁', '个人主页', '個人資料', '个人资料', 'プロフィール')

                            async def is_account_link(profile) -> bool:
                                if not await profile.is_visible() or await profile.evaluate("el => !!el.closest('article,[role=\"article\"]')"):
                                    return False
                                try:
                                    href = await profile.get_attribute('href')
                                    parsed = urlparse(urljoin(browser.page.url, href or ''))
                                    return (parsed.scheme in {'http', 'https'} and parsed.hostname in THREADS_HOSTS
                                            and not parsed.username and not parsed.password
                                            and parsed.port in {None, 80, 443}
                                            and bool(re.fullmatch(r'/@[^/\s]+/?', parsed.path)))
                                except (ValueError, TypeError):
                                    return False

                            for profile_name in profile_names:
                                profiles = browser.page.get_by_role('link', name=profile_name, exact=True)
                                for profile_index in range(await profiles.count()):
                                    if await is_account_link(profiles.nth(profile_index)):
                                        return True
                            # Threads can expose both SVG aria-label and <title> in
                            # the link's accessible name. Require the observed named
                            # visible profile icon, not any arbitrary author link.
                            icon_links = browser.page.locator('a[href]:has(svg[role="img"][aria-label])')
                            for profile_index in range(await icon_links.count()):
                                profile = icon_links.nth(profile_index)
                                if not await is_account_link(profile):
                                    continue
                                icons = profile.locator('svg[role="img"][aria-label]')
                                for icon_index in range(await icons.count()):
                                    icon = icons.nth(icon_index)
                                    if await icon.is_visible() and await icon.get_attribute('aria-label') in profile_names:
                                        return True
                            return False
                        await tab.click(timeout=self.timeout(until))
            await self.pause(.2, until)
        return False

    async def _sample_feed(self, browser, phase: str, deadline: float) -> dict:
        includes, excludes = self.rules()
        report = {'runId': self.run_id, 'phase': phase, 'time': datetime.now(timezone.utc).isoformat(), 'fingerprint': fingerprint(includes, excludes),
                  'verifiedForYou': False, 'status': 'unavailable', 'sampled': 0, 'classifiable': 0, 'matched': 0, 'unknown': 0,
                  'ads': 0, 'overlap': 0, 'target': self.settings.feed_sample_size, 'rate': None, 'delta': None, 'ids': []}
        observed = set()
        try:
            if not await self.navigate(browser, THREADS_HOME, deadline) or not await self._verify_for_you(browser, deadline):
                report['reason'] = '無法確認「為你推薦」與個人檔案導覽登入證據 / Verified For You and account navigation unavailable'
            else:
                report['verifiedForYou'] = True
                while self.active(deadline) and report['sampled'] < self.settings.feed_sample_size:
                    for card in await self.cards(browser, deadline, wait=True):
                        if card['id'] in observed:
                            continue
                        observed.add(card['id'])
                        if card.get('sponsored'):
                            report['ads'] += 1
                            continue
                        if phase == 'after' and card['id'] in self.final_ids:
                            report['overlap'] += 1
                            continue
                        report['ids'].append(card['id'])
                        report['sampled'] += 1
                        # Reports deliberately ignore local positive/negative overrides:
                        # comparable rates use the same text rules in both phases.
                        outcome = classify(card.get('text',''), includes, excludes)
                        if outcome == 'unknown':
                            report['unknown'] += 1
                        else:
                            report['classifiable'] += 1
                            report['matched'] += int(outcome == 'matched')
                        if report['sampled'] >= self.settings.feed_sample_size:
                            break
                    if self.active(deadline) and report['sampled'] < self.settings.feed_sample_size:
                        await browser.show_if_requested()
                        await browser.page.mouse.wheel(0, 650)
                        await self.pause(.5, deadline)
                report['status'] = 'complete' if report['sampled'] >= self.settings.feed_sample_size else 'partial'
                if report['classifiable']:
                    report['rate'] = round(100*report['matched']/report['classifiable'], 1)
                report['lowSample'] = report['classifiable'] < 5
            if phase == 'before':
                self.final_ids = set(report['ids'])
            else:
                report['delta'] = compare_reports(self.before, report)
        except Exception as exc:
            report['status'] = 'unavailable'
            report['reason'] = type(exc).__name__
            self.stats['errors'] += 1
        if self.stop_signal.is_set():
            report['status'] = 'stopped'
            report['delta'] = None
        try:
            self.content.report(phase, report)
        except OSError as exc:
            self.log(str(exc))
        self.log(f"{phase} 首頁抽樣：{report['status']}，{report['sampled']} 篇；僅為關鍵字代理指標，非演算法效果證明。")
        return report

    async def _collect_visible_post_urls(self, page, limit: int) -> list[str]:
        cards = await page.evaluate(EXTRACT_POSTS_JS)
        return collect_post_urls([card['url'] for card in cards], page.url, limit)

    async def _sleep_interruptibly(self, seconds: int) -> None:
        await self.pause(seconds, self.deadline)


STATE = AppState()


def parse_settings(payload: dict[str, object]) -> TrainerSettings:
    payload = sanitize_snapshot(payload)
    for key in NUMERIC_FIELDS:
        value = payload.get(key)
        if value is not None and (isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, str) and value.strip().lstrip("-").isdigit()))):
            raise ValueError("請輸入完整整數設定。")
    topics_raw = str(payload.get("topics", ""))
    topics = [line.strip() for line in topics_raw.splitlines() if line.strip()]
    if not topics:
        raise ValueError("請至少輸入一個主題。")

    settings = TrainerSettings(
        topics=topics,
        url_template=str(payload.get("urlTemplate", DEFAULT_URL_TEMPLATE)),
        seconds_per_topic=int(payload.get("secondsPerTopic", 35)),
        scrolls_per_topic=int(payload.get("scrollsPerTopic", 7)),
        posts_per_topic=int(payload.get("postsPerTopic", 3)),
        cooldown_seconds=int(payload.get("cooldownSeconds", 8)),
        session_minutes=int(payload.get("sessionMinutes", 20)),
        headless=bool(payload.get("headless", False)),
        include_keywords=str(payload.get("includeKeywords", "")),
        exclude_keywords=str(payload.get("excludeKeywords", "")),
        feed_check=payload.get("feedCheck", True),
        feed_sample_size=int(payload.get("feedSampleSize", 20)),
    )

    if settings.seconds_per_topic < 10:
        raise ValueError("每主題秒數至少 10 秒。")
    if settings.scrolls_per_topic < 1:
        raise ValueError("滾動次數至少 1 次。")
    if settings.posts_per_topic < 0:
        raise ValueError("每主題開啟貼文數不能小於 0。")
    if settings.cooldown_seconds < 0:
        raise ValueError("冷卻秒數不能小於 0。")
    if settings.session_minutes < 1:
        raise ValueError("總分鐘數至少 1 分鐘。")

    for key, (minimum, maximum) in NUMERIC_FIELDS.items():
        if not minimum <= int(payload.get(key, {"secondsPerTopic": 35, "scrollsPerTopic": 7, "postsPerTopic": 3, "cooldownSeconds": 8, "sessionMinutes": 20, "feedSampleSize": 20}[key])) <= maximum:
            raise ValueError("數字設定超出允許範圍。")
    return settings


def run_worker(settings: TrainerSettings, stop_signal: StopSignal, control: BrowserControl) -> None:
    trainer = ThreadsTrainer(settings, STATE.add_log, stop_signal, STATE.set_current_url, control, STATE.set_browser_state)
    STATE.mark_running(True)
    try:
        asyncio.run(trainer.run())
    except Exception as exc:  # noqa: BLE001 - shown in local UI
        STATE.set_browser_state("error", trainer.browser.headless if trainer.browser else settings.headless, str(exc))
        STATE.add_log(f"錯誤：{exc}")
    finally:
        STATE.mark_running(False)
        STATE.add_log("可以再次開始。")


async def open_manual_window(stop_signal: StopSignal, control: BrowserControl,
                             url: str, login: bool = False) -> None:
    if async_playwright is None:
        raise RuntimeError("缺少 Playwright。請先安裝 requirements.txt 與 Chromium。")
    async with async_playwright() as playwright:
        browser = ManagedBrowser(playwright, PROFILE_DIR, False, control,
                                 STATE.set_current_url, STATE.set_browser_state,
                                 STATE.add_log, stop_signal)
        try:
            await browser.start()
            try:
                await browser.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            except PlaywrightTimeoutError:
                STATE.add_log("專用瀏覽器頁面載入逾時，仍可手動操作。")
            await browser._focus()
            if login:
                STATE.add_log("登入視窗已開啟。完成 Threads 登入後，回到控制台按「完成登入」。")
            else:
                STATE.add_log("專用瀏覽器已開啟，使用與登入檢查相同的登入狀態。")
            while not stop_signal.is_set() and not browser.page.is_closed():
                await browser.show_if_requested()
                await asyncio.sleep(0.1)
        finally:
            await browser.close()


def run_manual_worker(stop_signal: StopSignal, control: BrowserControl,
                      url: str, login: bool = False) -> None:
    try:
        asyncio.run(open_manual_window(stop_signal, control, url, login))
    except Exception as exc:
        STATE.set_browser_state("error", False, str(exc))
        STATE.add_log(f"無法開啟專用瀏覽器：{exc}")
    finally:
        STATE.mark_running(False)
        STATE.add_log("專用瀏覽器已關閉。")


def run_login_worker(stop_signal: StopSignal, control: BrowserControl) -> None:
    run_manual_worker(stop_signal, control, THREADS_HOME, login=True)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ThreadsFeedTrainer/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self._send_html(render_index_html())
            return

        if self.path == "/api/settings":
            self._send_json(STORE.snapshot(retry=True))
            return

        if self.path == "/api/state":
            self._send_json(STATE.snapshot())
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path == "/api/feedback":
            try:
                payload = self._read_json()
                CONTENT.feedback(payload.get("id"), payload.get("value"))
                self._send_json({"ok": True})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if self.path == "/api/settings":
            try:
                payload = self._read_json()
                if "draft" not in payload and "ui" not in payload:
                    raise ValueError("缺少設定內容。")
                result = STORE.save(payload.get("draft"), ui=payload.get("ui"), draft_present="draft" in payload, client_id=payload.get("clientId"), sequence=payload.get("sequence"))
                self._send_json(result)
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        if self.path == "/api/start":
            self._handle_start()
            return

        if self.path == "/api/login":
            self._handle_login()
            return

        if self.path == "/api/browser":
            self._handle_browser()
            return

        if self.path == "/api/stop":
            self._handle_stop()
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def _handle_start(self) -> None:
        try:
            payload = self._read_json()
            settings = parse_settings(payload)
        except Exception as exc:  # noqa: BLE001 - user-facing validation
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        with STATE.lock:
            if STATE.running:
                self._send_json({"ok": False, "error": "目前已在執行中。"}, HTTPStatus.CONFLICT)
                return
            STATE.running = True
            STATE.mode = "browsing"
            STATE.browse_stopped = False
            stop_signal = StopSignal()
            control = BrowserControl()
            STATE.stop_signal = stop_signal
            STATE.browser_control = control
            STATE.browser = {"status": "starting", "headless": settings.headless, "error": None}
        try:
            saved = STORE.save(payload, checkpoint=True)
        except Exception as exc:
            STATE.mark_running(False)
            STATE.set_browser_state("closed", False)
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        worker = threading.Thread(
            target=run_worker,
            args=(settings, stop_signal, control),
            daemon=True,
        )
        with STATE.lock:
            STATE.stop_signal = stop_signal
            STATE.worker_thread = worker
            STATE.current_url = THREADS_HOME
            STATE.running = True
        STATE.add_log("準備啟動。")
        worker.start()
        self._send_json({"ok": True, "settings": saved})

    def _handle_login(self) -> None:
        with STATE.lock:
            if STATE.running:
                self._send_json({"ok": False, "error": "目前已在執行中。"}, HTTPStatus.CONFLICT)
                return
            STATE.running = True
            STATE.mode = "login"
            stop_signal = StopSignal()
            control = BrowserControl()
            STATE.stop_signal = stop_signal
            STATE.browser_control = control
            STATE.browser = {"status": "starting", "headless": False, "error": None}

        worker = threading.Thread(
            target=run_login_worker,
            args=(stop_signal, control),
            daemon=True,
        )
        with STATE.lock:
            STATE.stop_signal = stop_signal
            STATE.worker_thread = worker
            STATE.current_url = THREADS_HOME
            STATE.running = True
        STATE.add_log("準備開啟登入視窗。")
        worker.start()
        self._send_json({"ok": True})

    def _handle_browser(self) -> None:
        with STATE.lock:
            if STATE.running:
                control = STATE.browser_control
                if control is None:
                    self._send_json({"ok": False, "error": "瀏覽器尚未準備好，請稍後重試。"}, HTTPStatus.CONFLICT)
                    return
                control.show_requested.set()
                STATE.browser = {**STATE.browser, "status": "opening", "error": None}
                self._send_json({"ok": True})
                return
            STATE.running = True
            STATE.mode = "browser"
            stop_signal = StopSignal()
            control = BrowserControl()
            STATE.stop_signal = stop_signal
            STATE.browser_control = control
            STATE.browser = {"status": "starting", "headless": False, "error": None}
            worker = threading.Thread(target=run_manual_worker,
                                      args=(stop_signal, control, STATE.current_url), daemon=True)
            STATE.worker_thread = worker
        worker.start()
        self._send_json({"ok": True})

    def _handle_stop(self) -> None:
        with STATE.lock:
            stop_signal = STATE.stop_signal

        if stop_signal:
            stop_signal.stop()
            STATE.add_log("收到停止要求，正在收尾。")

        self._send_json({"ok": True})

    def _read_json(self) -> dict[str, object]:
        content_length = int(self.headers.get("Content-Length", "0"))
        if not 0 <= content_length <= 100000:
            raise ValueError("請求內容過大。")
        body = self.rfile.read(content_length)
        if not body:
            return {}
        value = json.loads(body.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("請求必須是物件。")
        return value

    def _send_json(
        self,
        payload: dict[str, object],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def find_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    port = find_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), RequestHandler)
    url = f"http://127.0.0.1:{port}"
    STATE.add_log(f"控制台已啟動：{url}")
    print(f"{APP_TITLE} running at {url}", flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def render_index_html() -> str:
    return Path(__file__).with_name("trainer_ui.html").read_text(encoding="utf-8")


if __name__ == "__main__":
    main()
