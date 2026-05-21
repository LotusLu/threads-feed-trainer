# Threads Feed Trainer

Threads Feed Trainer is a local Python utility for nudging your Threads recommendations toward topics you intentionally choose.

It opens Threads topic search pages in a dedicated Chromium profile, scrolls through the results, and optionally opens visible posts for short reading sessions. The tool is designed for low-frequency, manual, transparent use: it does not like, comment, repost, follow accounts, scrape private data, or submit login credentials.

## Why This Exists

Recommendation feeds are often shaped by accidental browsing. This project gives you a small local control panel for running deliberate topic-browsing sessions, so your recent activity has a clearer signal.

It does not guarantee any specific ranking outcome. Threads may change how its recommendation systems, search routes, or web UI behave at any time.

## Features

- Local web dashboard served on `127.0.0.1`
- Dedicated persistent Chromium profile for Threads login state
- Topic list input, one topic per line
- Configurable session length, dwell time, scroll count, post count, and cooldown
- Human-paced browsing with randomized scroll distances and pauses
- Optional visible-post visits from each topic search page
- Stop control that exits at the next safe pause
- Runtime logs shown in the local dashboard
- No automated engagement actions

## Requirements

- Python 3.10+
- Chromium installed through Playwright
- A Threads account, if Threads requires login in your region/session

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
python threads_trainer.py
```

The command opens a local dashboard in your default browser. From there, click the login/check-account action first if you need to sign in to Threads.

## Usage

1. Enter the topics you want to reinforce, one per line.

   ```text
   Python
   AI tools
   Taiwan travel
   Photography
   ```

2. Adjust session settings:
   - seconds per topic
   - scrolls per topic
   - posts to open per topic
   - cooldown between topics
   - total session minutes

3. Click the login/check-account action and sign in manually in the Chromium window.
4. Return to the dashboard and mark login complete.
5. Start the session.
6. Stop whenever needed; the worker will shut down at the next safe pause.

## Configuration Notes

The default search URL template is:

```text
https://www.threads.com/search?q={query}
```

`{query}` is replaced with the URL-encoded topic. If Threads changes its search route, update the template in the dashboard.

The browser profile is stored locally in `.browser-profile/`. Remove that directory if you want to clear the saved login session.

## Privacy and Safety

Threads Feed Trainer runs on your machine and uses a local browser profile. It does not send your topics, logs, or login state to any project server.

Use conservative settings. High-volume automation can look abnormal to platforms and may violate product terms. This project intentionally avoids automated engagement actions, but you are responsible for how you use it.

## Development

Run the test suite with:

```bash
python -m unittest discover -s tests
```

The current tests cover settings validation and Threads post URL collection.

## Project Scope

This is a small local automation tool, not a Threads API client and not a growth/engagement bot. The intended behavior is limited to opening topic pages, scrolling, reading, and showing local execution logs.
