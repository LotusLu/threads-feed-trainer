from __future__ import annotations

import asyncio
import json
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
DEFAULT_URL_TEMPLATE = "https://www.threads.com/search?q={query}"
PROFILE_DIR = Path(__file__).resolve().parent / ".browser-profile"
POST_PATH_MARKER = "/post/"
VISIBLE_TEXT_LIMIT = 180
CATEGORY_PRESETS = {
    "wellbeing": {
        "zh": {
            "name": "平靜與身心健康",
            "description": "降低焦慮、建立健康的數位生活節奏。",
            "topics": [
                "遠離網路焦慮",
                "健康數位習慣",
                "正念生活",
                "專注力工具",
                "慢生活",
            ],
        },
        "en": {
            "name": "Calm and wellbeing",
            "description": "Reduce anxiety and build healthier digital habits.",
            "topics": [
                "internet anxiety relief",
                "healthy digital habits",
                "mindfulness",
                "focus tools",
                "slow living",
            ],
        },
    },
    "learning": {
        "zh": {
            "name": "學習與知識",
            "description": "讓推薦內容偏向可長期累積的知識。",
            "topics": [
                "深度學習方法",
                "閱讀筆記",
                "科學新知",
                "語言學習",
                "知識管理",
            ],
        },
        "en": {
            "name": "Learning",
            "description": "Nudge the feed toward useful, compounding knowledge.",
            "topics": [
                "learning how to learn",
                "reading notes",
                "science news",
                "language learning",
                "knowledge management",
            ],
        },
    },
    "creative": {
        "zh": {
            "name": "創作靈感",
            "description": "補充攝影、設計、寫作與創意練習。",
            "topics": [
                "攝影靈感",
                "設計思考",
                "寫作練習",
                "手作創意",
                "影像敘事",
            ],
        },
        "en": {
            "name": "Creative",
            "description": "Bring in photography, design, writing, and craft ideas.",
            "topics": [
                "photography inspiration",
                "design thinking",
                "writing practice",
                "creative craft",
                "visual storytelling",
            ],
        },
    },
    "local_life": {
        "zh": {
            "name": "在地生活",
            "description": "把近期訊號轉向城市、旅遊與生活提案。",
            "topics": [
                "台灣旅遊",
                "台北咖啡店",
                "城市散步",
                "在地文化",
                "週末活動",
            ],
        },
        "en": {
            "name": "Local life",
            "description": "Shift recent signals toward places, travel, and daily life.",
            "topics": [
                "Taiwan travel",
                "Taipei cafes",
                "city walks",
                "local culture",
                "weekend events",
            ],
        },
    },
    "technology": {
        "zh": {
            "name": "科技與工具",
            "description": "追蹤實用科技、開發與生產力工具。",
            "topics": [
                "Python",
                "AI 工具",
                "資料視覺化",
                "開源專案",
                "生產力工具",
            ],
        },
        "en": {
            "name": "Technology",
            "description": "Follow useful technology, development, and productivity tools.",
            "topics": [
                "Python",
                "AI tools",
                "data visualization",
                "open source projects",
                "productivity tools",
            ],
        },
    },
    "custom": {
        "zh": {
            "name": "自訂",
            "description": "保留目前文字，不套用預設主題。",
            "topics": [],
        },
        "en": {
            "name": "Custom",
            "description": "Keep the current text and write your own topics.",
            "topics": [],
        },
    },
}


def localized_category_presets(language: str) -> list[dict[str, object]]:
    lang = "zh" if language.startswith("zh") else "en"
    return [
        {
            "id": preset_id,
            "name": values[lang]["name"],
            "description": values[lang]["description"],
            "topics": list(values[lang]["topics"]),
        }
        for preset_id, values in CATEGORY_PRESETS.items()
    ]


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


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.logs: list[dict[str, str]] = []
        self.worker_thread: threading.Thread | None = None
        self.stop_signal: StopSignal | None = None
        self.running = False

    def add_log(self, message: str) -> None:
        with self.lock:
            self.logs.append({"time": time.strftime("%H:%M:%S"), "message": message})
            self.logs = self.logs[-300:]

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {"running": self.running, "logs": self.logs}

    def mark_running(self, running: bool) -> None:
        with self.lock:
            self.running = running


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


class ThreadsTrainer:
    def __init__(
        self,
        settings: TrainerSettings,
        log: Callable[[str], None],
        stop_signal: StopSignal,
    ) -> None:
        self.settings = settings
        self.log = log
        self.stop_signal = stop_signal

    async def run(self) -> None:
        if async_playwright is None:
            raise RuntimeError(
                "缺少 Playwright。請先執行：pip install -r requirements.txt && "
                "python -m playwright install chromium"
            )

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.settings.session_minutes * 60

        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(PROFILE_DIR),
                headless=self.settings.headless,
                viewport={"width": 1280, "height": 900},
                locale="zh-TW",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-default-browser-check",
                ],
            )
            page = context.pages[0] if context.pages else await context.new_page()

            try:
                await self._cycle_topics(page, deadline)
            finally:
                await context.close()

    async def _cycle_topics(self, page, deadline: float) -> None:
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

                url = self._topic_url(topic)
                self.log(f"開啟主題：{topic}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                except PlaywrightTimeoutError:
                    self.log(f"載入逾時，略過：{topic}")
                    continue

                await self._browse_topic(page, topic)
                await self._cooldown()

        self.log("流程已結束。")

    def _topic_url(self, topic: str) -> str:
        encoded_query = quote_plus(topic)
        template = self.settings.url_template.strip() or DEFAULT_URL_TEMPLATE
        if "{query}" not in template:
            template = DEFAULT_URL_TEMPLATE
        return template.format(query=encoded_query)

    async def _browse_topic(self, page, topic: str) -> None:
        await self._log_page_diagnostics(page, topic, "搜尋頁")
        seconds_left = self.settings.seconds_per_topic
        for index in range(self.settings.scrolls_per_topic):
            if self.stop_signal.is_set():
                return

            scroll_distance = random.randint(550, 950)
            await page.mouse.wheel(0, scroll_distance)
            self.log(f"{topic}：滾動 {index + 1}/{self.settings.scrolls_per_topic}")

            if index == 0:
                await self._open_topic_posts(page, topic)

            pause = min(seconds_left, random.randint(3, 7))
            if pause > 0:
                await self._sleep_interruptibly(pause)
                seconds_left -= pause

        if seconds_left > 0:
            self.log(f"{topic}：停留閱讀 {seconds_left} 秒")
            await self._sleep_interruptibly(seconds_left)

    async def _open_topic_posts(self, page, topic: str) -> None:
        if self.settings.posts_per_topic <= 0:
            return

        search_url = page.url
        post_urls = await self._collect_visible_post_urls(page, self.settings.posts_per_topic)
        if not post_urls:
            self.log(f"{topic}：沒有找到可開啟的貼文連結，繼續瀏覽搜尋頁。")
            return

        self.log(f"{topic}：找到 {len(post_urls)} 篇貼文，開始逐篇閱讀。")
        for index, post_url in enumerate(post_urls, start=1):
            if self.stop_signal.is_set():
                return

            self.log(f"{topic}：開啟貼文 {index}/{len(post_urls)}")
            try:
                await page.goto(post_url, wait_until="domcontentloaded", timeout=45_000)
                await self._log_page_diagnostics(page, topic, "貼文")
                await page.mouse.wheel(0, random.randint(280, 620))
                await self._sleep_interruptibly(random.randint(5, 11))
            except PlaywrightTimeoutError:
                self.log(f"{topic}：貼文載入逾時，略過。")

        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=45_000)
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
            await asyncio.sleep(0.1)


STATE = AppState()


def parse_settings(payload: dict[str, object]) -> TrainerSettings:
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

    return settings


def run_worker(settings: TrainerSettings, stop_signal: StopSignal) -> None:
    trainer = ThreadsTrainer(settings, STATE.add_log, stop_signal)
    STATE.mark_running(True)
    try:
        asyncio.run(trainer.run())
    except Exception as exc:  # noqa: BLE001 - shown in local UI
        STATE.add_log(f"錯誤：{exc}")
    finally:
        STATE.mark_running(False)
        STATE.add_log("可以再次開始。")


async def open_login_window(stop_signal: StopSignal) -> None:
    if async_playwright is None:
        raise RuntimeError(
            "缺少 Playwright。請先執行：pip install -r requirements.txt && "
            "python -m playwright install chromium"
        )

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="zh-TW",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-default-browser-check",
            ],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto("https://www.threads.com/", wait_until="domcontentloaded")
            STATE.add_log("登入視窗已開啟。請在 Chromium 裡完成 Instagram/Threads 登入。")
            STATE.add_log("登入完成後，回到控制台按「完成登入」。")
            while not stop_signal.is_set():
                await asyncio.sleep(0.5)
        finally:
            await context.close()


def run_login_worker(stop_signal: StopSignal) -> None:
    STATE.mark_running(True)
    try:
        asyncio.run(open_login_window(stop_signal))
    except Exception as exc:  # noqa: BLE001 - shown in local UI
        STATE.add_log(f"錯誤：{exc}")
    finally:
        STATE.mark_running(False)
        STATE.add_log("登入視窗已關閉，可以開始訓練。")


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "ThreadsFeedTrainer/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/" or self.path.startswith("/?"):
            self._send_html(render_index_html())
            return

        if self.path == "/api/state":
            self._send_json(STATE.snapshot())
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path == "/api/start":
            self._handle_start()
            return

        if self.path == "/api/login":
            self._handle_login()
            return

        if self.path == "/api/stop":
            self._handle_stop()
            return

        self.send_error(HTTPStatus.NOT_FOUND)

    def _handle_start(self) -> None:
        if STATE.snapshot()["running"]:
            self._send_json({"ok": False, "error": "目前已在執行中。"}, HTTPStatus.CONFLICT)
            return

        try:
            payload = self._read_json()
            settings = parse_settings(payload)
        except Exception as exc:  # noqa: BLE001 - user-facing validation
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return

        stop_signal = StopSignal()
        worker = threading.Thread(
            target=run_worker,
            args=(settings, stop_signal),
            daemon=True,
        )
        with STATE.lock:
            STATE.stop_signal = stop_signal
            STATE.worker_thread = worker
            STATE.running = True
        STATE.add_log("準備啟動。")
        worker.start()
        self._send_json({"ok": True})

    def _handle_login(self) -> None:
        if STATE.snapshot()["running"]:
            self._send_json({"ok": False, "error": "目前已在執行中。"}, HTTPStatus.CONFLICT)
            return

        stop_signal = StopSignal()
        worker = threading.Thread(
            target=run_login_worker,
            args=(stop_signal,),
            daemon=True,
        )
        with STATE.lock:
            STATE.stop_signal = stop_signal
            STATE.worker_thread = worker
            STATE.running = True
        STATE.add_log("準備開啟登入視窗。")
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
        body = self.rfile.read(content_length)
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))

    def _send_json(
        self,
        payload: dict[str, object],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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
    category_presets = {
        "zh": localized_category_presets("zh"),
        "en": localized_category_presets("en"),
    }
    return INDEX_HTML.replace(
        "__CATEGORY_PRESETS_JSON__",
        json.dumps(category_presets, ensure_ascii=False),
    )


INDEX_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Threads Feed Trainer</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f4ef;
      --panel: #fffdf8;
      --panel-strong: #faf8f1;
      --ink: #202725;
      --muted: #65716c;
      --line: #ded9cc;
      --line-soft: #ebe6d9;
      --primary: #1f6f63;
      --primary-soft: #edf5f1;
      --primary-ink: #ffffff;
      --danger: #a24b3f;
      --danger-soft: #f2ded8;
      --field: #faf8f1;
      --shadow: 0 18px 45px rgba(49, 66, 58, 0.08);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      min-height: 100vh;
    }
    main {
      width: min(1180px, calc(100vw - 32px));
      margin: 28px auto;
      display: grid;
      grid-template-columns: minmax(0, 1.15fr) minmax(320px, 0.85fr);
      gap: 16px;
    }
    header {
      grid-column: 1 / -1;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      margin-bottom: 2px;
    }
    .eyebrow {
      margin: 0 0 7px;
      color: var(--primary);
      font-size: 13px;
      font-weight: 720;
    }
    h1 {
      margin: 0;
      font-size: clamp(28px, 4vw, 42px);
      line-height: 1.2;
      font-weight: 760;
      letter-spacing: 0;
    }
    .subtitle {
      margin: 10px 0 0;
      color: var(--muted);
      font-size: 15px;
      line-height: 1.55;
      max-width: 680px;
    }
    .header-tools {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .language-select {
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 0 12px;
      background: var(--panel);
      color: var(--ink);
      font: inherit;
      font-size: 14px;
      outline: none;
    }
    .status {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 8px 13px;
      color: var(--muted);
      background: var(--panel);
      font-size: 14px;
      white-space: nowrap;
    }
    .status.running {
      color: var(--primary);
      border-color: color-mix(in srgb, var(--primary) 45%, var(--line));
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      box-shadow: var(--shadow);
    }
    .panel-heading {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 14px;
    }
    h2 {
      margin: 0;
      font-size: 18px;
      line-height: 1.25;
      letter-spacing: 0;
    }
    .helper {
      margin: 5px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.45;
    }
    label {
      display: block;
      font-size: 14px;
      color: var(--muted);
      margin-bottom: 6px;
    }
    textarea,
    input[type="number"],
    input[type="text"] {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--field);
      color: var(--ink);
      font: inherit;
      padding: 10px 11px;
      outline: none;
    }
    textarea:focus,
    input:focus,
    select:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 16%, transparent);
    }
    textarea {
      min-height: 250px;
      resize: vertical;
      line-height: 1.5;
    }
    .categories {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 9px;
      margin: 10px 0 16px;
    }
    .category {
      min-height: 76px;
      border: 1px solid var(--line-soft);
      border-radius: 7px;
      background: var(--panel-strong);
      color: var(--ink);
      padding: 10px;
      text-align: left;
      cursor: pointer;
      transition: border-color 160ms ease, background 160ms ease, transform 160ms ease;
    }
    .category:hover {
      border-color: color-mix(in srgb, var(--primary) 35%, var(--line));
      transform: translateY(-1px);
    }
    .category.active {
      border-color: var(--primary);
      background: var(--primary-soft);
      color: var(--primary);
    }
    .category-name {
      display: block;
      font-weight: 720;
      font-size: 13px;
      line-height: 1.25;
    }
    .category-desc {
      display: block;
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    .category.active .category-desc { color: color-mix(in srgb, var(--primary) 72%, var(--muted)); }
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .field { margin-bottom: 14px; }
    .toggle {
      display: flex;
      gap: 8px;
      align-items: center;
      color: var(--ink);
      margin: 4px 0 14px;
    }
    .toggle input { width: 18px; height: 18px; }
    .actions {
      display: flex;
      gap: 10px;
      margin-top: 16px;
      flex-wrap: wrap;
    }
    button {
      min-height: 40px;
      border: 1px solid transparent;
      border-radius: 6px;
      padding: 0 16px;
      font: inherit;
      font-weight: 650;
      cursor: pointer;
    }
    button:disabled {
      cursor: not-allowed;
      opacity: 0.55;
    }
    .start {
      background: var(--primary);
      color: var(--primary-ink);
    }
    .login {
      background: #e8f0ed;
      color: var(--primary);
      border-color: #d0ded8;
    }
    .stop {
      background: var(--danger-soft);
      color: var(--danger);
      border-color: #e7c8c0;
    }
    .secondary {
      background: var(--panel-strong);
      color: var(--ink);
      border-color: var(--line);
    }
    .note {
      border: 1px solid #d7ded6;
      border-radius: 7px;
      background: #f2f7f2;
      color: #3d5b50;
      padding: 12px;
      font-size: 13px;
      line-height: 1.5;
      margin-top: 12px;
    }
    .advanced {
      border-top: 1px solid var(--line-soft);
      padding-top: 14px;
      margin-top: 14px;
    }
    .log {
      min-height: 260px;
      max-height: 440px;
      overflow: auto;
      display: flex;
      flex-direction: column;
      gap: 8px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 13px;
      line-height: 1.45;
    }
    .log-line {
      display: grid;
      grid-template-columns: 68px minmax(0, 1fr);
      gap: 8px;
      border-bottom: 1px solid var(--line-soft);
      padding-bottom: 8px;
    }
    .time { color: var(--muted); }
    .message { overflow-wrap: anywhere; }
    .wide { grid-column: 1 / -1; }
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; }
      header { align-items: start; flex-direction: column; }
      .header-tools { justify-content: flex-start; }
      .categories { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 520px) {
      main { width: min(100vw - 20px, 1180px); margin-top: 18px; }
      section { padding: 14px; }
      .grid, .categories { grid-template-columns: 1fr; }
      .actions button { flex: 1 1 100%; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <p class="eyebrow" data-i18n="eyebrow">本機控制台</p>
        <h1>Threads Feed Trainer</h1>
        <p class="subtitle" data-i18n="heroSubtitle">用你主動選擇的主題，讓近期瀏覽訊號遠離網路焦慮與惡意資訊。</p>
      </div>
      <div class="header-tools">
        <select id="language" class="language-select" aria-label="Language">
          <option value="zh">繁中</option>
          <option value="en">English</option>
        </select>
        <div id="status" class="status">待命</div>
      </div>
    </header>

    <section>
      <div class="panel-heading">
        <div>
          <h2 data-i18n="topicsTitle">主題分類</h2>
          <p class="helper" data-i18n="topicsHelper">選一組常見分類後仍可自由編輯內容。</p>
        </div>
        <button id="restore" class="secondary" type="button" data-i18n="restore">恢復上次內容</button>
      </div>
      <div id="categories" class="categories"></div>
      <div class="field">
        <label for="topics" data-i18n="topicsLabel">主題（一行一個）</label>
        <textarea id="topics" spellcheck="false"></textarea>
      </div>
      <div class="note" data-i18n="privacyNote">這個工具不會記錄或送出你的帳號密碼。登入會在專用 Chromium 視窗中手動完成；主題與執行紀錄只保留在你的電腦上。</div>
    </section>

    <section>
      <div class="panel-heading">
        <div>
          <h2 data-i18n="settingsTitle">執行設定</h2>
          <p class="helper" data-i18n="settingsHelper">保持低頻、接近人工閱讀節奏。</p>
        </div>
      </div>
      <div class="grid">
        <div class="field">
          <label for="secondsPerTopic" data-i18n="secondsPerTopic">每主題秒數</label>
          <input id="secondsPerTopic" type="number" min="10" max="600" value="35">
        </div>
        <div class="field">
          <label for="scrollsPerTopic" data-i18n="scrollsPerTopic">滾動次數</label>
          <input id="scrollsPerTopic" type="number" min="1" max="80" value="7">
        </div>
        <div class="field">
          <label for="postsPerTopic" data-i18n="postsPerTopic">開啟貼文數</label>
          <input id="postsPerTopic" type="number" min="0" max="20" value="3">
        </div>
        <div class="field">
          <label for="cooldownSeconds" data-i18n="cooldownSeconds">冷卻秒數</label>
          <input id="cooldownSeconds" type="number" min="0" max="120" value="8">
        </div>
        <div class="field">
          <label for="sessionMinutes" data-i18n="sessionMinutes">總分鐘數</label>
          <input id="sessionMinutes" type="number" min="1" max="240" value="20">
        </div>
      </div>
      <label class="toggle">
        <input id="headless" type="checkbox">
        <span data-i18n="headless">背景執行</span>
      </label>
      <div class="advanced">
        <div class="field">
          <label for="urlTemplate" data-i18n="urlTemplate">URL 模板</label>
          <input id="urlTemplate" type="text" value="https://www.threads.com/search?q={query}">
          <p class="helper" data-i18n="urlHelper">保留 {query}，工具會替換成主題關鍵字。</p>
        </div>
      </div>
      <div class="actions">
        <button id="login" class="login" type="button" data-i18n="login">登入/檢查帳號</button>
        <button id="start" class="start" type="button" data-i18n="start">開始</button>
        <button id="stop" class="stop" type="button" disabled data-i18n="stop">停止</button>
      </div>
    </section>

    <section class="wide">
      <div class="panel-heading">
        <div>
          <h2 data-i18n="logTitle">狀態紀錄</h2>
          <p class="helper" data-i18n="logHelper">只顯示本機執行過程，不會上傳到專案伺服器。</p>
        </div>
      </div>
      <div id="log" class="log"></div>
    </section>
  </main>

  <script>
    const CATEGORY_PRESETS = __CATEGORY_PRESETS_JSON__;
    const STORAGE_KEY = "threadsFeedTrainer.settings.v2";
    const TRANSLATIONS = {
      zh: {
        eyebrow: "本機控制台",
        heroSubtitle: "用你主動選擇的主題，讓近期瀏覽訊號遠離網路焦慮與惡意資訊。",
        ready: "待命",
        running: "執行中",
        topicsTitle: "主題分類",
        topicsHelper: "選一組常見分類後仍可自由編輯內容。",
        restore: "恢復上次內容",
        topicsLabel: "主題（一行一個）",
        privacyNote: "這個工具不會記錄或送出你的帳號密碼。登入會在專用 Chromium 視窗中手動完成；主題與執行紀錄只保留在你的電腦上。",
        settingsTitle: "執行設定",
        settingsHelper: "保持低頻、接近人工閱讀節奏。",
        secondsPerTopic: "每主題秒數",
        scrollsPerTopic: "滾動次數",
        postsPerTopic: "開啟貼文數",
        cooldownSeconds: "冷卻秒數",
        sessionMinutes: "總分鐘數",
        headless: "背景執行",
        urlTemplate: "URL 模板",
        urlHelper: "保留 {query}，工具會替換成主題關鍵字。",
        login: "登入/檢查帳號",
        start: "開始",
        stop: "停止",
        loginDone: "完成登入",
        logTitle: "狀態紀錄",
        logHelper: "只顯示本機執行過程，不會上傳到專案伺服器。",
        requestFailed: "請求失敗",
      },
      en: {
        eyebrow: "Local control panel",
        heroSubtitle: "Use topics you choose intentionally to move recent browsing signals away from internet anxiety and malicious information.",
        ready: "Ready",
        running: "Running",
        topicsTitle: "Topic categories",
        topicsHelper: "Choose a common set, then edit the topics freely.",
        restore: "Restore last session",
        topicsLabel: "Topics (one per line)",
        privacyNote: "This tool does not record or submit your account password. Login happens manually in a dedicated Chromium window; topics and runtime logs stay on your computer.",
        settingsTitle: "Session settings",
        settingsHelper: "Keep sessions low-frequency and close to a human reading pace.",
        secondsPerTopic: "Seconds per topic",
        scrollsPerTopic: "Scrolls per topic",
        postsPerTopic: "Posts to open",
        cooldownSeconds: "Cooldown seconds",
        sessionMinutes: "Total minutes",
        headless: "Run in background",
        urlTemplate: "URL template",
        urlHelper: "Keep {query}; the tool replaces it with each topic keyword.",
        login: "Login / check account",
        start: "Start",
        stop: "Stop",
        loginDone: "Login complete",
        logTitle: "Activity log",
        logHelper: "Shows only local runtime activity; it is not uploaded to a project server.",
        requestFailed: "Request failed",
      },
    };

    const fields = {
      topics: document.querySelector("#topics"),
      urlTemplate: document.querySelector("#urlTemplate"),
      secondsPerTopic: document.querySelector("#secondsPerTopic"),
      scrollsPerTopic: document.querySelector("#scrollsPerTopic"),
      postsPerTopic: document.querySelector("#postsPerTopic"),
      cooldownSeconds: document.querySelector("#cooldownSeconds"),
      sessionMinutes: document.querySelector("#sessionMinutes"),
      headless: document.querySelector("#headless"),
    };
    const languageSelect = document.querySelector("#language");
    const categoriesEl = document.querySelector("#categories");
    const restoreButton = document.querySelector("#restore");
    const loginButton = document.querySelector("#login");
    const startButton = document.querySelector("#start");
    const stopButton = document.querySelector("#stop");
    const statusEl = document.querySelector("#status");
    const logEl = document.querySelector("#log");
    let selectedLanguage = "zh";
    let selectedCategory = "wellbeing";
    let lastSavedSettings = null;

    function inferLanguage() {
      const languages = Array.from(navigator.languages || [navigator.language || ""]);
      return languages.some((language) => {
        const normalized = String(language).toLowerCase();
        return normalized === "zh" || normalized.startsWith("zh-");
      }) ? "zh" : "en";
    }

    function safeReadSettings() {
      try {
        const raw = window.localStorage.getItem(STORAGE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        return parsed && typeof parsed === "object" ? parsed : null;
      } catch {
        return null;
      }
    }

    function safeSaveSettings(settings) {
      lastSavedSettings = settings;
      try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
      } catch {
        // localStorage can be unavailable in private or locked-down browsers.
      }
    }

    function currentTranslation() {
      return TRANSLATIONS[selectedLanguage] || TRANSLATIONS.zh;
    }

    function currentPresets() {
      return CATEGORY_PRESETS[selectedLanguage] || CATEGORY_PRESETS.zh;
    }

    function topicsForCategory(categoryId) {
      const preset = currentPresets().find((item) => item.id === categoryId);
      return preset ? preset.topics.join("\n") : "";
    }

    function applyTranslations() {
      const t = currentTranslation();
      document.documentElement.lang = selectedLanguage === "zh" ? "zh-Hant" : "en";
      document.querySelectorAll("[data-i18n]").forEach((node) => {
        const key = node.getAttribute("data-i18n");
        if (key && t[key]) node.textContent = t[key];
      });
      if (!STATE_RUNNING) {
        statusEl.textContent = t.ready;
      }
      if (stopButton.dataset.mode === "login") {
        stopButton.textContent = t.loginDone;
      }
    }

    function renderCategories() {
      categoriesEl.innerHTML = currentPresets().map((preset) => `
        <button class="category ${preset.id === selectedCategory ? "active" : ""}" type="button" data-category="${preset.id}">
          <span class="category-name">${escapeHtml(preset.name)}</span>
          <span class="category-desc">${escapeHtml(preset.description)}</span>
        </button>
      `).join("");
    }

    function settingsSnapshot() {
      return {
        language: selectedLanguage,
        category: selectedCategory,
        topics: fields.topics.value,
        urlTemplate: fields.urlTemplate.value,
        secondsPerTopic: Number(fields.secondsPerTopic.value),
        scrollsPerTopic: Number(fields.scrollsPerTopic.value),
        postsPerTopic: Number(fields.postsPerTopic.value),
        cooldownSeconds: Number(fields.cooldownSeconds.value),
        sessionMinutes: Number(fields.sessionMinutes.value),
        headless: fields.headless.checked,
      };
    }

    function saveCurrentSettings() {
      safeSaveSettings(settingsSnapshot());
    }

    function applySettings(settings) {
      if (!settings || typeof settings !== "object") return false;
      selectedLanguage = ["zh", "en"].includes(settings.language) ? settings.language : inferLanguage();
      selectedCategory = typeof settings.category === "string" ? settings.category : "wellbeing";
      languageSelect.value = selectedLanguage;
      fields.topics.value = typeof settings.topics === "string" ? settings.topics : topicsForCategory(selectedCategory);
      fields.urlTemplate.value = typeof settings.urlTemplate === "string" ? settings.urlTemplate : "https://www.threads.com/search?q={query}";
      fields.secondsPerTopic.value = Number.isFinite(Number(settings.secondsPerTopic)) ? settings.secondsPerTopic : 35;
      fields.scrollsPerTopic.value = Number.isFinite(Number(settings.scrollsPerTopic)) ? settings.scrollsPerTopic : 7;
      fields.postsPerTopic.value = Number.isFinite(Number(settings.postsPerTopic)) ? settings.postsPerTopic : 3;
      fields.cooldownSeconds.value = Number.isFinite(Number(settings.cooldownSeconds)) ? settings.cooldownSeconds : 8;
      fields.sessionMinutes.value = Number.isFinite(Number(settings.sessionMinutes)) ? settings.sessionMinutes : 20;
      fields.headless.checked = Boolean(settings.headless);
      return true;
    }

    let STATE_RUNNING = false;

    function payload() {
      return {
        topics: fields.topics.value,
        urlTemplate: fields.urlTemplate.value,
        secondsPerTopic: Number(fields.secondsPerTopic.value),
        scrollsPerTopic: Number(fields.scrollsPerTopic.value),
        postsPerTopic: Number(fields.postsPerTopic.value),
        cooldownSeconds: Number(fields.cooldownSeconds.value),
        sessionMinutes: Number(fields.sessionMinutes.value),
        headless: fields.headless.checked,
      };
    }

    async function postJson(path, body = {}) {
      const response = await fetch(path, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body),
      });
      const data = await response.json();
      if (!response.ok || data.ok === false) {
        throw new Error(data.error || currentTranslation().requestFailed);
      }
      return data;
    }

    async function refresh() {
      const response = await fetch("/api/state");
      const state = await response.json();
      STATE_RUNNING = state.running;
      statusEl.textContent = state.running ? currentTranslation().running : currentTranslation().ready;
      statusEl.classList.toggle("running", state.running);
      loginButton.disabled = state.running;
      startButton.disabled = state.running;
      stopButton.disabled = !state.running;
      logEl.innerHTML = state.logs.map((entry) => `
        <div class="log-line">
          <span class="time">${entry.time}</span>
          <span class="message">${escapeHtml(entry.message)}</span>
        </div>
      `).join("");
      logEl.scrollTop = logEl.scrollHeight;
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    categoriesEl.addEventListener("click", (event) => {
      const button = event.target.closest("[data-category]");
      if (!button) return;
      selectedCategory = button.dataset.category;
      if (selectedCategory !== "custom") {
        fields.topics.value = topicsForCategory(selectedCategory);
      }
      renderCategories();
      saveCurrentSettings();
    });

    Object.values(fields).forEach((field) => {
      field.addEventListener("input", () => {
        if (field === fields.topics) selectedCategory = "custom";
        renderCategories();
        saveCurrentSettings();
      });
      field.addEventListener("change", saveCurrentSettings);
    });

    languageSelect.addEventListener("change", () => {
      selectedLanguage = languageSelect.value;
      applyTranslations();
      renderCategories();
      saveCurrentSettings();
    });

    restoreButton.addEventListener("click", () => {
      const settings = safeReadSettings() || lastSavedSettings;
      if (applySettings(settings)) {
        applyTranslations();
        renderCategories();
        saveCurrentSettings();
      }
    });

    startButton.addEventListener("click", async () => {
      try {
        stopButton.dataset.mode = "stop";
        stopButton.textContent = currentTranslation().stop;
        saveCurrentSettings();
        await postJson("/api/start", payload());
        await refresh();
      } catch (error) {
        alert(error.message);
      }
    });

    loginButton.addEventListener("click", async () => {
      try {
        stopButton.dataset.mode = "login";
        stopButton.textContent = currentTranslation().loginDone;
        saveCurrentSettings();
        await postJson("/api/login");
        await refresh();
      } catch (error) {
        alert(error.message);
      }
    });

    stopButton.addEventListener("click", async () => {
      try {
        await postJson("/api/stop");
        stopButton.dataset.mode = "stop";
        stopButton.textContent = currentTranslation().stop;
        await refresh();
      } catch (error) {
        alert(error.message);
      }
    });

    const storedSettings = safeReadSettings();
    selectedLanguage = storedSettings?.language && ["zh", "en"].includes(storedSettings.language)
      ? storedSettings.language
      : inferLanguage();
    languageSelect.value = selectedLanguage;
    if (!applySettings(storedSettings)) {
      selectedCategory = "wellbeing";
      fields.topics.value = topicsForCategory(selectedCategory);
    }
    applyTranslations();
    renderCategories();
    saveCurrentSettings();
    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
