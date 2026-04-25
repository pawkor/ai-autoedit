# Modern UI — NLE Design Spec
*2026-04-24*

## Kontekst

Legacy UI (`index.html`) pozostaje domyślnym trybem. Modern UI (`modern.html`) to opcjonalny tryb eksperymentalny dostępny przez przełącznik w Settings. Gemini's poprzednia próba implementacji Modern UI jest niedziałająca i zostaje zastąpiona.

## Cel

Modern UI oferuje NLE-style workflow (wzorowany na DaVinci Resolve / Final Cut Pro): Music-driven algorytm generuje draft timeline, użytkownik edytuje go przez drag & drop, następnie renderuje.

---

## Faza 1: Legacy Cleanup

### 1a — Revert zmian Gemini w legacy plikach

| Plik | Akcja |
|---|---|
| `webapp/static/js/ui.js` | Usunąć: `let jobs=[]` globalnie, `isModern` branching w `refreshJobList`, `setUiMode()`, `s-ui-mode` w `_readSettingsData`, zmiana labela w `_applyTraditionalMode`. **Zostawić:** null-guardy w `setLogFilter`. |
| `webapp/static/index.html` | Usunąć: `<script src="/js/mode_switcher.js">`, Privacy section (blur plates/speed — unimplementowane), `candidate-pool-hint`. **Zostawić:** Interface Mode select (`s-ui-mode`) w Settings. |
| `webapp/static/js/gallery/select.js` | Usunąć `isModern` branching. |
| `webapp/static/js/gallery/jobs.js` | Usunąć `isModern` branching. |
| `webapp/static/js/services/forms.js` | Usunąć `isModern` branching. |
| `webapp/server.py` | Zostawić FileResponse routes — przydatne dla `modern.html`. |
| `webapp/routers/jobs.py` | Zostawić privacy params — harmless. |

### 1b — Legacy dual-mode cleanup

- **Advanced modal:** dodać wizualny separator przed threshold/min-scene-len oznaczony "Traditional mode" — params już ukryte domyślnie, tylko lepszy podział wizualny
- **Settings panel:** usunąć `js-no-music` checkbox (ukryty, bezużyteczny przy Music-driven default)
- Render footer pozostaje bez zmian (Music-driven już jest primary)

**Szacowany czas:** ~3h

---

## Faza 2: Modern UI v1

### Scope v1

- ✅ NLE ekran (pool + timeline)
- ✅ Music selection (lista, pin, rebuild)
- ❌ Preview player — v2
- ❌ Render flow — v2
- ❌ Results — v2

### Zasada

Modern UI to izolowana wyspa. `modern.html` ładuje wyłącznie `modern_*.js`. Zero importów z legacy JS.

### Pliki

```
webapp/static/
  modern.html          ← pełny rewrite
  css/modern.css       ← zachować tokeny kolorów, wyrzucić martwy CSS
  js/
    modern.js          ← główna logika NLE (~500 linii)
    modern_music.js    ← music list + pin + rebuild trigger
    mode_switcher.js   ← bez zmian
```

### Layout

```
┌─────────────────────────────────────────────────────────────┐
│ TOP BAR: [● projekt] [────────────] [▶ Preview] [⬡ Render] │
├──────────┬──────────────────────────────────────┬───────────┤
│          │  POOL SCEN                           │  MUZYKA   │
│ SIDEBAR  │  grid miniatur, CLIP score border    │  lista    │
│ projekty │  drag source → timeline              │  pin/     │
│          │──────────────────────────────────────│  unpin    │
│          │  TIMELINE                            │           │
│          │  [klip][klip][klip]…                 │  Tryb:    │
│          │  beat markers                        │  ♪ MD     │
│          │  muzyka waveform label               │           │
│          │  [↺ zmień muzykę → przebuduj]        │  [Render] │
└──────────┴──────────────────────────────────────┴───────────┘
```

- Sidebar: 140px, lista projektów z `/api/jobs`
- Right panel: 175px, music list + pin + render button
- Pool: górna połowa main area, grid z `minmax(76px,1fr)`
- Timeline: dolna połowa, horizontal scroll, stała wysokość ~210px

### Kolory scen w pool

| CLIP score | Border kolor |
|---|---|
| ≥ 0.85 | `#22c55e` (bright green) |
| 0.7–0.85 | `#4ade80` (mid green) |
| 0.5–0.7 | `#facc15` (yellow) |
| < 0.5 | `#475569` (slate, nieaktywny) |

---

## Data Flow

### 1. Ładowanie projektu

```
GET /api/jobs/{id}          → params (threshold, selected_track)
GET /api/jobs/{id}/frames   → [ { scene, score, duration, path, file_start } ]
GET /api/music?job_id={id}  → { tracks: [ { file, title, bpm, duration } ] }
```

### 2. Draft timeline

```
POST /api/jobs/{id}/md-preview          ← NOWY endpoint
body: { music_file, threshold }
response: [ { scene, duration, clip_path, score, beat_index } ]
```

Dry-run `music_driven.match_clips()` bez ffmpeg. Zwraca kolejność scen dopasowaną do muzyki. Czas odpowiedzi: ~50–200ms.

### 3. Edycja użytkownika

Tylko lokalny JS state — nic nie trafia do backendu do momentu Render.

Operacje:
- **Drag pool → timeline:** wstaw/zamień scenę w danym slocie
- **Drag timeline → timeline:** reorder
- **× na klipu:** usuń ze timeline → dodaj do `overrides.ban`
- Usunięty klip pozostaje w pool (można ponownie przeciągnąć)

### 4. Zmiana muzyki

```
1. Użytkownik klika inny track w right panel → pin
2. POST /api/jobs/{id}/md-preview z nowym music_file
3. Replace timeline state
4. Update waveform label
```

### 5. Render (v1)

```
POST /api/jobs/{id}/render-music-driven
body: { selected_track, overrides }
```

Render w v1 nie czeka na wynik — pokazuje komunikat "Render started, sprawdź wyniki w Legacy UI".

---

## Nowy backend endpoint: `md-preview`

```python
# webapp/routers/jobs.py

class MdPreviewParams(BaseModel):
    music_file: str
    threshold: float = 0.148

@router.post("/{job_id}/md-preview")
async def md_preview(job_id: str, body: MdPreviewParams):
    """Dry-run music-driven: zwraca kolejność scen bez renderowania."""
    job = get_job(job_id)
    clips = await run_in_executor(music_driven.match_clips_dry, job, body.music_file, body.threshold)
    return [{"scene": c.scene, "duration": c.duration, "score": c.score, "beat_index": c.beat_index} for c in clips]
```

Wymaga: wyodrębnienie logiki selekcji z `music_driven.py` jako `match_clips_dry()` zwracającej listę bez wywoływania ffmpeg. Zakres: istniejąca `match_clips()` prawdopodobnie wymaga minimalnych zmian (ffmpeg wywoływany osobno po selekcji) — do weryfikacji podczas implementacji.

---

## Drag & Drop

Natywne HTML5 DnD — bez bibliotek zewnętrznych.

```js
// Pool → timeline
thumbnail.addEventListener('dragstart', e => {
    e.dataTransfer.setData('scene', scene_id);
    e.dataTransfer.setData('source', 'pool');
});

timelineSlot.addEventListener('dragover', e => e.preventDefault());
timelineSlot.addEventListener('drop', e => {
    const scene = e.dataTransfer.getData('scene');
    insertSceneAtSlot(scene, slotIndex);
    rebuildTimeline();
});

// Timeline → timeline (reorder)
timelineClip.addEventListener('dragstart', e => {
    e.dataTransfer.setData('source', 'timeline');
    e.dataTransfer.setData('from_index', clipIndex);
});
```

---

## Czego Modern v1 NIE robi

- Brak in-browser video preview (pool miniatury to obrazki, nie wideo)
- Brak preview playback sekwencji
- Render → komunikat "started", nie czeka na wynik
- Brak uploadu YT/IG
- Brak Settings modal (link do Legacy Settings)

---

## Faza 3: Modern v2 (oddzielny plan)

- Preview player (sekwencja miniatur lub video proxy)
- Render progress w Modern UI
- Results z odtwarzaczem
- Hover video preview w pool

---

## Szacowany czas

| Faza | Zakres | Czas |
|---|---|---|
| 1 — Legacy cleanup | Revert Gemini + dual-mode | ~3h |
| 2 — Modern v1 | modern.html + CSS + JS + md-preview endpoint | ~2 dni |
| 3 — Modern v2 | Preview, Render, Results | oddzielny plan |
