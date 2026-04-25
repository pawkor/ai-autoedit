# Modern UI Phase 5 — Shorts Interface Design

**Date:** 2026-04-25

## Goal

Add native Shorts generation to Modern UI. Right-panel button opens a modal; modal closes on submit; progress shows in the existing top-bar status bar.

## Architecture

New file `webapp/static/js/modern_shorts.js` handles the Shorts modal (open/close/submit). Loaded after `modern_analyze.js`, before `modern.js`. All functions exported on `window.*`.

`modern.js` `_onJobMessage` extended to handle `shorts_status` and `shorts_batch_progress` WebSocket messages — updates top-bar phase/progress without interfering with main render state.

## Components

### Right panel button

New `▶ Shorts` button in `#m-panel`, below `⚙ Settings`, above `⚙ Legacy UI`. Calls `openShortsModal()`. Disabled when no project is selected (same guard as Render button).

### Shorts modal (`#m-shorts-modal`)

Fields:
- **Count** — number input 1–9, default 1
- **Text overlays** — checkbox (`shorts_text`)
- **Multicam** — checkbox (`shorts_multicam`), hidden when `job.params.cameras` has ≤ 1 entry
- **Beat sync** — checkbox (`shorts_beat_sync`)
- **Best of best** — checkbox (`shorts_best`)

Footer: status span + **⬡ Make Shorts** button.

On open: pre-fills all fields from `job.params` (persisted values).

On submit (`renderShorts()`):
1. Disable button, set status "Starting…"
2. `PATCH /api/jobs/{id}/params` — persist `{shorts_text, shorts_multicam, shorts_beat_sync, shorts_best}` (requires backend allowlist extension — see Files table)
3. `POST /api/jobs/{id}/render-short` — trigger generation with `{count, best}`
4. Close modal
5. Top bar picks up progress via WebSocket

### WebSocket handling in `modern.js`

`_onJobMessage` currently ignores `shorts_status` and `shorts_batch_progress`. Add:

- `shorts_status {running: true}` → `_showStatus('shorts', 'Generating short…', null, 'running')`
- `shorts_status {running: false, done: true}` → brief "✓ Short ready" flash + `loadResults()`
- `shorts_status {running: false, error: true}` → brief "✗ Shorts failed" flash
- `shorts_batch_progress {pct, done, total}` → `_showStatus('shorts', 'Short N/M', pct, 'running')`

Top-bar phase label shows "shorts" during generation, separate from main render phase.

## Data Flow

```
openShortsModal()
  → GET /api/jobs/{id}          (pre-fill from job.params)
  → render modal

renderShorts()
  → PATCH /api/jobs/{id}/params  (persist settings)
  → POST /api/jobs/{id}/render-short
  → closeModal
  → WS: shorts_status {running:true}  → top bar "Generating short…"
  → WS: shorts_batch_progress         → top bar "Short 1/3 …%"
  → WS: shorts_status {running:false, done:true}
      → flash "✓ Short ready"
      → loadResults() — Results modal Shorts tab updates
```

## Files

| Action | File |
|--------|------|
| Create | `webapp/static/js/modern_shorts.js` |
| Modify | `webapp/static/modern.html` — modal HTML + panel button + script tag |
| Modify | `webapp/static/css/modern.css` — minimal new rules (reuse existing tokens) |
| Modify | `webapp/static/js/modern.js` — `shorts_status` / `shorts_batch_progress` handling in `_onJobMessage` |
| Modify | `webapp/routers/jobs.py` — extend `PATCH /api/jobs/{id}/params` allowlist with `shorts_text`, `shorts_multicam`, `shorts_beat_sync`, `shorts_best` |

## Out of Scope

NCS music checkbox, shorts music dir override, crop offsets — available via Settings if needed.

YouTube and Instagram upload from Results modal — Phase 6 (separate spec). YT modal requires playlist picker, description editor, Claude meta generation, upload progress polling. IG modal requires attribution pre-fill, token expiry warning.
