# Threads Feed Trainer

## 中文說明

Threads Feed Trainer 是一個在本機執行的 Python 小工具，幫助你用「主動選擇的主題」調整近期 Threads 瀏覽訊號，讓推薦內容遠離網路焦慮、惡意資訊、情緒操弄與偶然點擊造成的資訊迴圈。

它會在專用 Chromium 瀏覽器設定檔中開啟 Threads 搜尋頁，依照你選的主題慢速瀏覽、滾動，並可短暫開啟可見貼文閱讀。這個工具設計成低頻、透明、手動啟動的本機控制台；它不會按讚、留言、轉發、追蹤帳號、爬取私人資料，也不會記錄或送出你的帳號密碼。

### 主要功能

- 在 `127.0.0.1` 開啟本機控制台
- 支援繁體中文 / English 介面，首次開啟會依電腦瀏覽器語系預設
- 常見主題分類可一鍵套用，也能自由編輯
- 自動記住上次使用的分類、主題與執行設定
- 使用專用 Chromium profile 保存 Threads 登入狀態
- 可調整總時長、每主題停留秒數、滾動次數、開啟貼文數與冷卻秒數
- 以接近人工閱讀的節奏瀏覽，包含隨機滾動距離與停留時間
- 本機顯示執行紀錄
- 不執行任何自動互動或成長駭客操作

### 安裝需求

- Python 3.10+
- Playwright 安裝的 Chromium
- Threads 帳號（若 Threads 在你的地區或工作階段要求登入）

### 安裝與啟動

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python threads_trainer.py
```

啟動後會自動在預設瀏覽器開啟本機控制台。

### 使用方式

1. 在控制台選擇語言。
2. 選一個主題分類，例如「平靜與身心健康」、「學習與知識」或「科技與工具」。
3. 視需要編輯主題清單，每行一個主題。
4. 第一次使用時，按「登入/檢查帳號」，在 Chromium 視窗中手動完成 Threads/Instagram 登入。
5. 登入完成後回到控制台，按「完成登入」關閉登入視窗。
6. 調整停留秒數、滾動次數、開啟貼文數、冷卻秒數與總分鐘數。
7. 按「開始」執行。需要中止時按「停止」。

### 設定說明

預設 Threads 搜尋 URL 模板是：

```text
https://www.threads.com/search?q={query}
```

`{query}` 會被替換成 URL 編碼後的主題。如果 Threads 更改搜尋網址，可以在控制台調整這個模板。

瀏覽器登入狀態保存在本機 `.browser-profile/`。如果你想清除登入狀態，刪除這個資料夾即可。控制台的上次使用內容保存在瀏覽器 `localStorage`，只留在你的電腦上。

### 隱私與安全

Threads Feed Trainer 在你的電腦上執行，不會把主題、紀錄、登入狀態或密碼送到專案伺服器。登入是在專用 Chromium 視窗中由你手動完成。

請保守使用。高頻率自動化可能看起來不正常，也可能違反平台條款。本專案刻意避免自動互動行為，但你仍需自行負責使用方式。

### 開發

執行測試：

```bash
python -m unittest discover -s tests
```

### 專案範圍

這是一個小型本機自動化工具，不是 Threads API client，也不是成長、導流或互動機器人。預期行為只包含開啟主題頁、滾動、短暫閱讀可見貼文，以及顯示本機執行紀錄。

## English

Threads Feed Trainer is a local Python utility that helps you use intentionally chosen topics to shape recent Threads browsing signals away from internet anxiety, malicious information, emotional manipulation, and accidental recommendation loops.

It opens Threads search pages in a dedicated Chromium profile, slowly scrolls through selected topics, and can briefly open visible posts for reading. The tool is designed for low-frequency, transparent, manually started local use. It does not like, comment, repost, follow accounts, scrape private data, or record or submit your account password.

### Features

- Local dashboard served on `127.0.0.1`
- Traditional Chinese / English UI, with first launch inferred from browser language
- Common topic categories that can be applied and edited
- Last-used category, topics, and session settings saved locally
- Dedicated persistent Chromium profile for Threads login state
- Configurable total duration, dwell time, scroll count, posts to open, and cooldown
- Human-paced browsing with randomized scroll distances and pauses
- Local runtime logs in the dashboard
- No automated engagement actions

### Requirements

- Python 3.10+
- Chromium installed through Playwright
- A Threads account, if Threads requires login in your region or session

### Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python threads_trainer.py
```

The command opens the local dashboard in your default browser.

### Usage

1. Choose the dashboard language.
2. Select a topic category, such as Calm and wellbeing, Learning, or Technology.
3. Edit the topic list if needed, one topic per line.
4. On first use, click Login / check account and sign in manually in the Chromium window.
5. Return to the dashboard and click Login complete to close the login window.
6. Adjust dwell time, scroll count, posts to open, cooldown, and total minutes.
7. Click Start. Click Stop whenever you want to end the session.

### Configuration

The default Threads search URL template is:

```text
https://www.threads.com/search?q={query}
```

`{query}` is replaced with the URL-encoded topic. If Threads changes its search route, update the template in the dashboard.

The browser login profile is stored locally in `.browser-profile/`. Remove that directory if you want to clear the saved login session. The dashboard's last-used content is stored in browser `localStorage` and remains on your machine.

### Privacy and Safety

Threads Feed Trainer runs on your computer. It does not send your topics, logs, login state, or password to any project server. Login is completed manually by you in a dedicated Chromium window.

Use conservative settings. High-volume automation can look abnormal to platforms and may violate product terms. This project intentionally avoids automated engagement actions, but you are responsible for how you use it.

### Development

Run the test suite with:

```bash
python -m unittest discover -s tests
```

### Project Scope

This is a small local automation tool, not a Threads API client and not a growth or engagement bot. The intended behavior is limited to opening topic pages, scrolling, briefly reading visible posts, and showing local execution logs.
