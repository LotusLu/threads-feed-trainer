# Threads Feed Trainer Dashboard Refresh Design

## Goal

Refresh the local Threads Feed Trainer dashboard so it feels calm, intentional, and polished without looking like a generic AI-generated interface. The tool should communicate its purpose clearly: helping users step away from internet anxiety and malicious or low-quality information by browsing topics they choose deliberately.

## Approved Direction

Use the "Quiet Utility" visual direction:

- Warm off-white background with restrained green accents.
- Compact, professional panels with subtle borders and modest 6-8px radii.
- Clear hierarchy, readable spacing, and no decorative glow/orb/AI-style effects.
- Tool-first layout: categories and topic editing are primary, session controls are secondary, logs remain visible and practical.

## User-Facing Features

### Topic Categories

Replace the current always-visible default topic text with selectable common categories. Selecting a category fills the topic textarea with a recommended editable topic list.

Initial categories:

- Calm and wellbeing
- Learning
- Creative
- Local life
- Technology
- Custom

The Custom category preserves the user's current textarea content instead of overwriting it. Users can edit any category's generated topics before starting a session.

### Last Used Content

Save the user's last-used settings in browser `localStorage`, including:

- Selected language
- Selected category
- Topic textarea
- URL template
- Session numeric settings
- Headless toggle

On page load, restore the saved settings when present. If no saved language exists, infer the default language from `navigator.languages` or `navigator.language`: Traditional Chinese for `zh`, `zh-TW`, `zh-HK`, and `zh-Hant`; English otherwise.

### Privacy Copy

Add a visible privacy note near the topics and login controls:

- The tool does not record or submit the user's account password.
- Login happens manually in the dedicated Chromium profile.
- Topics and logs remain local to the user's machine.

The text should be available in both supported languages.

### Multilingual UI

Support Traditional Chinese and English for all dashboard labels, helper text, category names, buttons, status text, and client-side error fallback text.

Implementation approach:

- Keep translation dictionaries in the existing embedded frontend JavaScript.
- Mark translatable DOM elements with stable data attributes.
- Re-render labels when the user switches language.
- Store the selected language in `localStorage`.

Runtime logs from Python can remain Traditional Chinese for this iteration because they are produced by backend automation paths. UI labels around the log should still translate.

### README

Rewrite README as bilingual Traditional Chinese and English documentation. It should explain:

- The project helps users move away from internet anxiety, malicious information, and accidental recommendation loops.
- It is a local, transparent utility for intentionally browsing chosen topics.
- It does not like, comment, repost, follow, scrape private data, or submit login credentials.
- Requirements, installation, usage, privacy/safety notes, development test command, and project scope.

## Layout

Use a responsive two-column dashboard on desktop:

- Header: product name, short value statement, language selector, running status.
- Main left panel: category chooser, topic textarea, last-used hint/action, privacy note.
- Right panel: session settings, login/start/stop actions, advanced URL template.
- Bottom full-width panel: activity log.

On narrow screens, stack sections into one column. Controls must not overflow or overlap.

## Data Flow

1. Page load determines language and settings from `localStorage`.
2. If settings exist, restore them.
3. If no settings exist, apply the default category topics for the inferred language.
4. Category selection updates the selected category and topic textarea.
5. Any user edit to topics or settings schedules a save to `localStorage`.
6. Start sends the current payload to `/api/start` unchanged.
7. Existing backend validation continues to enforce runtime safety.

## Error Handling

- If `localStorage` read/write fails, the dashboard continues with in-memory defaults.
- If a stored settings payload is malformed, ignore it and use defaults.
- If an unsupported language is stored, fall back to inferred language.
- Backend API errors continue to display via `alert`, but the fallback text should be translated.

## Testing

Add focused tests for any Python behavior changed by the work. Since category presets, localStorage, and i18n are browser-side, the minimum verification is:

- Existing Python unit tests pass.
- Manual browser check confirms:
  - Visual layout matches Quiet Utility direction.
  - Language defaults from browser language and can be switched.
  - Last-used settings persist after refresh.
  - Category selection fills editable topics.
  - Privacy note is visible.
  - Start payload still reaches existing backend validation.

## Non-Goals

- No account credential storage.
- No backend user settings file.
- No extra framework or build step.
- No automated engagement features.
- No broad refactor outside the existing single-file local app.
