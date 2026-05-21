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
            self._send_html(INDEX_HTML)
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


INDEX_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Threads Feed Trainer</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f7f8;
      --panel: #ffffff;
      --ink: #1c2429;
      --muted: #63727a;
      --line: #d8e0e4;
      --primary: #146c63;
      --primary-ink: #ffffff;
      --danger: #aa3a32;
      --field: #fbfcfc;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
    }
    main {
      width: min(1120px, calc(100vw - 32px));
      margin: 24px auto;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 340px;
      gap: 18px;
    }
    header {
      grid-column: 1 / -1;
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
    }
    h1 {
      margin: 0;
      font-size: 28px;
      line-height: 1.2;
      font-weight: 700;
      letter-spacing: 0;
    }
    .status {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 7px 12px;
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
      padding: 16px;
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
    input:focus {
      border-color: var(--primary);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--primary) 16%, transparent);
    }
    textarea {
      min-height: 320px;
      resize: vertical;
      line-height: 1.5;
    }
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
    }
    button {
      min-height: 40px;
      border: 0;
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
      background: #e8f0ef;
      color: var(--primary);
    }
    .stop {
      background: #f5dedb;
      color: var(--danger);
    }
    .log {
      min-height: 430px;
      max-height: 560px;
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
      border-bottom: 1px solid #eef2f3;
      padding-bottom: 8px;
    }
    .time { color: var(--muted); }
    .message { overflow-wrap: anywhere; }
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; }
      header { align-items: start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>Threads Feed Trainer</h1>
      <div id="status" class="status">待命</div>
    </header>

    <section>
      <div class="field">
        <label for="topics">主題</label>
        <textarea id="topics" spellcheck="false">Python
AI tools
資料視覺化
攝影
台灣旅遊</textarea>
      </div>
      <div class="field">
        <label for="urlTemplate">URL 模板</label>
        <input id="urlTemplate" type="text" value="https://www.threads.com/search?q={query}">
      </div>
    </section>

    <section>
      <div class="grid">
        <div class="field">
          <label for="secondsPerTopic">每主題秒數</label>
          <input id="secondsPerTopic" type="number" min="10" max="600" value="35">
        </div>
        <div class="field">
          <label for="scrollsPerTopic">滾動次數</label>
          <input id="scrollsPerTopic" type="number" min="1" max="80" value="7">
        </div>
        <div class="field">
          <label for="postsPerTopic">開啟貼文數</label>
          <input id="postsPerTopic" type="number" min="0" max="20" value="3">
        </div>
        <div class="field">
          <label for="cooldownSeconds">冷卻秒數</label>
          <input id="cooldownSeconds" type="number" min="0" max="120" value="8">
        </div>
        <div class="field">
          <label for="sessionMinutes">總分鐘數</label>
          <input id="sessionMinutes" type="number" min="1" max="240" value="20">
        </div>
      </div>
      <label class="toggle">
        <input id="headless" type="checkbox">
        背景執行
      </label>
      <div class="actions">
        <button id="login" class="login" type="button">登入/檢查帳號</button>
        <button id="start" class="start" type="button">開始</button>
        <button id="stop" class="stop" type="button" disabled>停止</button>
      </div>
    </section>

    <section style="grid-column: 1 / -1;">
      <label>狀態</label>
      <div id="log" class="log"></div>
    </section>
  </main>

  <script>
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
    const loginButton = document.querySelector("#login");
    const startButton = document.querySelector("#start");
    const stopButton = document.querySelector("#stop");
    const statusEl = document.querySelector("#status");
    const logEl = document.querySelector("#log");

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
        throw new Error(data.error || "Request failed");
      }
      return data;
    }

    async function refresh() {
      const response = await fetch("/api/state");
      const state = await response.json();
      statusEl.textContent = state.running ? "執行中" : "待命";
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

    startButton.addEventListener("click", async () => {
      try {
        stopButton.textContent = "停止";
        await postJson("/api/start", payload());
        await refresh();
      } catch (error) {
        alert(error.message);
      }
    });

    loginButton.addEventListener("click", async () => {
      try {
        stopButton.textContent = "完成登入";
        await postJson("/api/login");
        await refresh();
      } catch (error) {
        alert(error.message);
      }
    });

    stopButton.addEventListener("click", async () => {
      try {
        await postJson("/api/stop");
        stopButton.textContent = "停止";
        await refresh();
      } catch (error) {
        alert(error.message);
      }
    });

    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
