# Dashboard Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the approved calm bilingual dashboard refresh with category presets, local last-used settings, privacy copy, and bilingual README.

**Architecture:** Keep the existing single-file local Python app. Add small Python constants/helpers for category defaults so unit tests cover the topic preset source of truth, then render the embedded HTML from those constants. Browser-side JavaScript handles i18n, category selection, localStorage persistence, and existing API calls.

**Tech Stack:** Python 3 standard library HTTP server, Playwright automation, embedded HTML/CSS/JavaScript, `unittest`.

---

### Task 1: Category Preset Source

**Files:**
- Modify: `threads_trainer.py`
- Test: `tests/test_threads_trainer.py`

- [ ] **Step 1: Write the failing tests**

Add tests that import `CATEGORY_PRESETS` and `localized_category_presets`, then assert there are bilingual default categories and that returned data is copied:

```python
from threads_trainer import (
    CATEGORY_PRESETS,
    DEFAULT_URL_TEMPLATE,
    collect_post_urls,
    localized_category_presets,
    parse_settings,
)


class CategoryPresetTests(unittest.TestCase):
    def test_category_presets_include_bilingual_wellbeing_topics(self):
        wellbeing = CATEGORY_PRESETS["wellbeing"]

        self.assertEqual(wellbeing["zh"]["name"], "平靜與身心健康")
        self.assertIn("遠離網路焦慮", wellbeing["zh"]["topics"])
        self.assertEqual(wellbeing["en"]["name"], "Calm and wellbeing")
        self.assertIn("healthy digital habits", wellbeing["en"]["topics"])

    def test_localized_category_presets_returns_independent_lists(self):
        presets = localized_category_presets("en")
        presets[0]["topics"].append("mutated")

        fresh_presets = localized_category_presets("en")

        self.assertNotIn("mutated", fresh_presets[0]["topics"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m unittest tests.test_threads_trainer.CategoryPresetTests`

Expected: FAIL or ERROR because `CATEGORY_PRESETS` and `localized_category_presets` do not exist yet.

- [ ] **Step 3: Implement category constants and helper**

Add `CATEGORY_PRESETS` near the top of `threads_trainer.py`, plus:

```python
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
```

Include category ids `wellbeing`, `learning`, `creative`, `local_life`, `technology`, and `custom`.

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m unittest tests.test_threads_trainer.CategoryPresetTests`

Expected: PASS.

### Task 2: Dashboard UI, i18n, and Persistence

**Files:**
- Modify: `threads_trainer.py`

- [ ] **Step 1: Replace the embedded `INDEX_HTML`**

Update `INDEX_HTML` to:

- Use the Quiet Utility visual style.
- Render category presets from `localized_category_presets` data embedded as JSON.
- Add language selector with `zh` and `en`.
- Add category cards, topic textarea, privacy note, settings, URL template, actions, and log panel.
- Add JavaScript dictionaries for all dashboard labels in Traditional Chinese and English.
- Infer default language from `navigator.languages` / `navigator.language`.
- Store and restore last-used settings in `localStorage`.
- Preserve existing `/api/login`, `/api/start`, `/api/stop`, `/api/state` behavior.

- [ ] **Step 2: Run the Python tests**

Run: `python -m unittest discover -s tests`

Expected: all tests pass.

### Task 3: README Rewrite

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Rewrite README bilingually**

Replace README with Traditional Chinese first and English second. Include purpose, privacy/safety, requirements, installation, usage, configuration notes, development tests, and project scope.

- [ ] **Step 2: Review README rendering**

Run: `sed -n '1,260p' README.md`

Expected: README contains both `## 中文說明` and `## English`.

### Task 4: Browser Verification

**Files:**
- No source files modified.

- [ ] **Step 1: Start the local app**

Run: `python threads_trainer.py`

Expected: terminal prints `Threads Feed Trainer running at http://127.0.0.1:<port>`.

- [ ] **Step 2: Check the page in a browser**

Verify:

- The dashboard uses the Quiet Utility visual direction.
- Language can switch between 繁中 and English.
- A first load uses browser language when no stored language exists.
- Category cards fill editable topics.
- Refresh restores last-used settings.
- Privacy note says passwords are not recorded or submitted.
- API buttons remain present and state polling works.

- [ ] **Step 3: Stop the app**

Stop the running app process cleanly.

### Task 5: Final Verification

**Files:**
- No source files modified unless verification exposes a defect.

- [ ] **Step 1: Run all tests**

Run: `python -m unittest discover -s tests`

Expected: all tests pass.

- [ ] **Step 2: Inspect git diff**

Run: `git diff --stat && git diff -- threads_trainer.py README.md tests/test_threads_trainer.py`

Expected: diff is limited to planned dashboard, README, and test changes.
