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
from dataclasses import dataclass
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
                  "sessionMinutes": (1, 240)}


def sanitize_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("設定必須是物件。")
    result: dict[str, object] = {}
    for key, item in value.items():
        if key in {"topics", "urlTemplate"}:
            if not isinstance(item, str) or len(item) > (50000 if key == "topics" else 2048):
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
        elif key == "headless":
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
                     "ui": {"topicsCollapsed": False, "logCollapsed": False}}
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
        return {key: value.get(key, False) for key in ("topicsCollapsed", "logCollapsed")}

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
            return {"running": self.running, "mode": self.mode, "currentUrl": self.current_url, "browser": dict(self.browser), "logs": list(self.logs)}

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
                self.mode = "idle"


def collect_post_urls(raw_hrefs: list[str], base_url: str, limit: int) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    base_host = urlparse(base_url).netloc

    for href in raw_hrefs:
        absolute_url = urljoin(base_url, href)
        parsed = urlparse(absolute_url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if parsed.netloc != base_host:
            continue
        if POST_PATH_MARKER not in parsed.path:
            continue

        clean_url = urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")
        )
        if clean_url in seen:
            continue

        seen.add(clean_url)
        urls.append(clean_url)
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

    async def run(self) -> None:
        if async_playwright is None:
            raise RuntimeError(
                "缺少 Playwright。請先執行：pip install -r requirements.txt && "
                "python -m playwright install chromium"
            )

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.settings.session_minutes * 60

        async with async_playwright() as playwright:
            self.browser = ManagedBrowser(playwright, PROFILE_DIR, self.settings.headless,
                                          self.control, self.on_page_change, self.on_browser_state,
                                          self.log, self.stop_signal)
            try:
                await self.browser.start()
                await self._cycle_topics(self.browser, deadline)
            finally:
                await self.browser.close()

    async def _cycle_topics(self, browser, deadline: float) -> None:
        cycle_count = 0
        self.log("瀏覽器已啟動。如果尚未登入 Threads，請在視窗中手動登入。")

        while not self.stop_signal.is_set() and time.monotonic() < deadline:
            topics = self.settings.topics[:]
            random.shuffle(topics)
            cycle_count += 1
            self.log(f"開始第 {cycle_count} 輪，共 {len(topics)} 個主題。")

            for topic in topics:
                if self.stop_signal.is_set() or time.monotonic() >= deadline:
                    break

                await browser.show_if_requested()
                url = self._topic_url(topic)
                self.log(f"開啟主題：{topic}")
                try:
                    await browser.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                except PlaywrightTimeoutError:
                    self.log(f"載入逾時，略過：{topic}")
                    continue

                await browser.show_if_requested()
                await self._browse_topic(browser, topic)
                await self._cooldown()

        self.log("流程已結束。")

    def _topic_url(self, topic: str) -> str:
        encoded_query = quote_plus(topic)
        template = self.settings.url_template.strip() or DEFAULT_URL_TEMPLATE
        if "{query}" not in template:
            template = DEFAULT_URL_TEMPLATE
        return template.format(query=encoded_query)

    async def _browse_topic(self, browser, topic: str) -> None:
        await self._log_page_diagnostics(browser.page, topic, "搜尋頁")
        await browser.show_if_requested()
        seconds_left = self.settings.seconds_per_topic
        for index in range(self.settings.scrolls_per_topic):
            if self.stop_signal.is_set():
                return

            await browser.show_if_requested()
            scroll_distance = random.randint(550, 950)
            await browser.page.mouse.wheel(0, scroll_distance)
            self.log(f"{topic}：滾動 {index + 1}/{self.settings.scrolls_per_topic}")

            if index == 0:
                await self._open_topic_posts(browser, topic)

            pause = min(seconds_left, random.randint(3, 7))
            if pause > 0:
                await self._sleep_interruptibly(pause)
                seconds_left -= pause

        if seconds_left > 0:
            self.log(f"{topic}：停留閱讀 {seconds_left} 秒")
            await self._sleep_interruptibly(seconds_left)

    async def _open_topic_posts(self, browser, topic: str) -> None:
        if self.settings.posts_per_topic <= 0:
            return

        search_url = browser.page.url
        post_urls = await self._collect_visible_post_urls(browser.page, self.settings.posts_per_topic)
        if not post_urls:
            self.log(f"{topic}：沒有找到可開啟的貼文連結，繼續瀏覽搜尋頁。")
            return

        self.log(f"{topic}：找到 {len(post_urls)} 篇貼文，開始逐篇閱讀。")
        for index, post_url in enumerate(post_urls, start=1):
            if self.stop_signal.is_set():
                return

            await browser.show_if_requested()
            self.log(f"{topic}：開啟貼文 {index}/{len(post_urls)}")
            try:
                await browser.page.goto(post_url, wait_until="domcontentloaded", timeout=45_000)
                await browser.show_if_requested()
                await self._log_page_diagnostics(browser.page, topic, "貼文")
                await browser.show_if_requested()
                await browser.page.mouse.wheel(0, random.randint(280, 620))
                await self._sleep_interruptibly(random.randint(5, 11))
            except PlaywrightTimeoutError:
                self.log(f"{topic}：貼文載入逾時，略過。")

        await browser.show_if_requested()
        try:
            await browser.page.goto(search_url, wait_until="domcontentloaded", timeout=45_000)
        except PlaywrightTimeoutError:
            self.log(f"{topic}：返回搜尋頁逾時。")

    async def _collect_visible_post_urls(self, page, limit: int) -> list[str]:
        raw_hrefs = await page.locator("a[href]").evaluate_all(
            "(links) => links.map((link) => link.href || link.getAttribute('href') || '')"
        )
        return collect_post_urls(raw_hrefs, page.url, limit)

    async def _log_page_diagnostics(self, page, topic: str, label: str) -> None:
        try:
            title = await page.title()
            text = await page.locator("body").inner_text(timeout=5_000)
        except PlaywrightTimeoutError:
            self.log(f"{topic}：{label} 診斷逾時。")
            return

        summary = summarize_visible_text(text)
        self.log(f"{topic}：{label} URL {page.url}")
        if title:
            self.log(f"{topic}：{label} 標題 {title}")
        if summary:
            self.log(f"{topic}：{label} 可見內容 {summary}")

    async def _cooldown(self) -> None:
        if self.settings.cooldown_seconds <= 0:
            return
        self.log(f"冷卻 {self.settings.cooldown_seconds} 秒")
        await self._sleep_interruptibly(self.settings.cooldown_seconds)

    async def _sleep_interruptibly(self, seconds: int) -> None:
        for _ in range(seconds * 10):
            if self.stop_signal.is_set():
                return
            if self.browser is not None:
                await self.browser.show_if_requested()
            await asyncio.sleep(0.1)


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
        if not minimum <= int(payload.get(key, {"secondsPerTopic": 35, "scrollsPerTopic": 7, "postsPerTopic": 3, "cooldownSeconds": 8, "sessionMinutes": 20}[key])) <= maximum:
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
