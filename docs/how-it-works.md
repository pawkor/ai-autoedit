# Jak to działa / How it works

## PL

Pipeline zamienia surowy materiał z całego dnia w highlight reel bez ręcznego montażu. Uruchamiany przez przeglądarkę, wyniki dostępne w zakładce Results bez kopiowania plików.

### Kroki pipeline — Analyze (wspólne dla obu trybów)

| Krok | Opis |
|------|------|
| 1 | Znalezienie plików MP4 w katalogu roboczym |
| 2 | Detekcja cięć — PySceneDetect `detect-content` **lub** CLIP-first (skanowanie klatek co N s, peaki CLIP) |
| 3 | Podział — każda scena jako osobny plik w `_autoframe/autocut/` (stream copy) |
| 4 | Ekstrakcja 3 klatek per klip (_f0/_f1/_f2 = 25/50/75%) → `_autoframe/frames/` |
| 5 | Scoring CLIP — `ViT-L-14` na GPU → `scene_scores.csv` / `scene_scores_allcam.csv` |
| 5b | GPS annotation (opcjonalne) — exiftool extraktuje ścieżkę GPS z plików Insta360; prędkość i kąt obrotu per scena dodawane do CSV jako `gps_speed_max`, `gps_turn_max`; blendowane ze score CLIP gdy `gps_weight > 0` |

Wyniki kroków 1–5 są cache'owane — ponowne uruchomienie pomija już przetworzone etapy.

### Kroki pipeline — ♪ Music-driven (tryb domyślny)

| Krok | Opis |
|------|------|
| 6 | Analiza muzyki — librosa: beaty, energia → harmonogram cięć zsynchronizowany z rytmem; długość slotu = beats_fast/mid/slow beatów per segment |
| 7 | Analiza ruchu klipów — OpenCV frame diff dla top-N klipów wg CLIP score |
| 8 | Dopasowanie klipów do slotów — rank: CLIP×0.50 + ruch×0.30 + łuk chronologiczny×0.20; naprzemienne kamery wg wzorca `cam_pattern` |
| 9 | Trimming + enkodowanie każdego klipu (NVENC) → concat → miks muzyczny |
| 10 | Intro (klatka z najwyższym score CLIP, pełna rozdzielczość) + outro + fade |
| 11 | Wynik: `YYYY-MM-Miejsce-DD-md_v1.mp4` — kolejne rundy `md_v2`, `md_v3…` |

### Kroki pipeline — ▶ Render Highlight (Traditional mode)

| Krok | Opis |
|------|------|
| 6 | Selekcja scen — threshold CLIP, manualne overrides, balansowanie kamer, per-file cap |
| 7 | Przycinanie + enkodowanie wybranych scen → concat → `_autoframe/highlight.mp4` |
| 8 | Intro + outro + fade → `_autoframe/highlight_final.mp4` |
| 9 | Dobór muzyki wg BPM/energii, miks → `YYYY-MM-Miejsce-DD_v1.mp4` |

### Detekcja scen — CLIP-first (domyślna)

Domyślna metoda. Nie szuka cięć montażowych — szuka dobrego materiału. Skanuje klatki co N sekund (domyślnie 3s), oblicza score CLIP dla każdej klatki, wykrywa lokalne maxima (peaki) i wycina klip (domyślnie 8s) wokół każdego piku. Minimalna przerwa między klipami (domyślnie 30s) zapobiega nakładaniu.

Wyniki: pliki `-clip-NNN` w `autocut/`. Wymagane dla music-driven multicam — `score_all_cams` scoruje wszystkie kamery → `scene_scores_allcam.csv`.

### Detekcja scen — PySceneDetect (opcjonalna)

Dostępna gdy CLIP-first jest wyłączony w Advanced modal. Wykrywa cięcia przez różnice histogramów kolorów (`detect-content`). ffprobe wykrywa rzeczywisty fps i przekazuje przez `--frame-rate` — naprawia błędne timecody w plikach VFR (np. kamery tylne z niestandardowym `time_base`).

Parametry: `threshold` (domyślnie 20), `min_scene_len` (domyślnie 8s). Wyniki: pliki `-scene-NNN` w `autocut/`.

Wyniki detekcji są cache'owane per plik — zmiana parametrów i Re-analyze przetwarza tylko pliki bez CSV.

### Scoring CLIP

Model `ViT-L-14` OpenCLIP (wagi OpenAI) na GPU. Klatki przetwarzane w paczkach (domyślnie 64). Dla każdej klatki:

```
pos_score   = średnie podobieństwo cosinusowe do wszystkich promptów pozytywnych
neg_score   = średnie podobieństwo cosinusowe do wszystkich promptów negatywnych
final_score = pos_score - neg_score × neg_weight
```

Wyniki trafiają do `_autoframe/scene_scores.csv` (główna kamera) lub `scene_scores_allcam.csv` (wszystkie kamery).

### Selekcja scen (Traditional mode)

Używana wyłącznie w trybie Traditional. Sceny filtrowane i wybierane osobno dla każdego pliku źródłowego:

- Tylko sceny powyżej `threshold` (ustawiany w Select scenes).
- Każdy plik ma limit `max_per_file_sec`.
- Każda scena przycinana do `max_scene_sec`, wyśrodkowana na środku klipu.
- Klipy krótsze niż `min_take_sec` po przycięciu odrzucane.
- Manualne overrides z Select scenes (force-include / force-exclude) mają pierwszeństwo.

### Dual-camera (multicam)

Gdy skonfigurowane są dwie kamery (np. kask + tył motocykla):

- Kamera A (kask, AUDIO_CAM) jest scorowana przez CLIP i stanowi podstawę selekcji.
- Kamera B (tył) nie jest scorowana — sceny dobierane przez timestamp matching (±30s).
- Timestamps obliczane z `Start Time (seconds)` w CSV PySceneDetect + fps z ffprobe (naprawia błąd ~10x w plikach VFR).
- Wybrane pary przeplatane: `helmet[1] → back[1] → helmet[2] → back[2] → …`
- Kamera B jest wyciszana; audio pochodzi wyłącznie z kamery A.

Gdy `score_all_cams=true` (automatyczne przy CLIP-first): wszystkie kamery scorowane przez CLIP → `scene_scores_allcam.csv`. Music-driven używa allcam CSV gdy istnieje.

Szacowany czas w Select scenes uwzględnia `cam_ratio` (stosunek łącznych scen do scen z głównej kamery) — estymacja jest dokładna nawet przed renderem dzięki background dry-run API.

### Enkodowanie

Wybrane sceny przycinane i re-encodowane do wspólnego formatu (libx264, aac 48kHz stereo, CFR) przed finalnym concat. Re-encoding audio eliminuje desynce na przejściach między kamerami (VFR source → CFR output). Finalne enkodowanie: skalowanie do 4K (Lanczos), 60fps CFR, NVENC jeśli dostępny.

4K jest celowe — YouTube przydziela znacznie więcej bitrate do uploadów 4K niż 1080p.

### Intro i outro

Tło intro: klatka z najwyższym score CLIP. Nad nią dwie linie fontem Caveat Bold: rok + nazwa trasy (auto z nazwy katalogu roboczego). Outro: czarna plansza z konfigurowalnym tekstem. Fade in/out. Montaż przez stream copy do `_autoframe/highlight_final.mp4`.

### Music-driven render

Tryb domyślny. Zamiast sekwencji timeline dobiera klipy pod strukturę muzyczną: podział na segmenty (intro/verse/chorus/outro), synchronizacja z beatami.

The default render mode. Instead of a timeline sequence, clips are matched to the music structure: segment split (intro/verse/chorus/outro), beat synchronisation.

```
src/music_driven.py
  load_audio()       → librosa beat/segment analysis
  match_clips()      → fill each segment with highest-scoring available clips
  render()           → ffmpeg concat + music mix
```

Różnorodność źródeł: `recent_sources` deque (maxlen = max(4, num_sources×2)) zapobiega skupieniu klipów z jednego pliku. Każda scena użyta max raz (`used` set).

Różnorodność kamer: wzorzec `cam_pattern` (np. `aabaab`) definiuje kolejność kamer — litery `a`/`b` odpowiadają Cam A / Cam B z Settings. Puste = score-driven alternation (najlepszy dostępny klip per slot, bez wymuszania kolejności). Gdy wzorzec jest aktywny, _desired_camera_ per slot pochodzi z wzorca cyklicznego.

Camera diversity: the `cam_pattern` field (e.g. `aabaab`) defines camera order — letters `a`/`b` map to Cam A / Cam B from Settings. Empty = score-driven alternation (best available clip per slot). When a pattern is active, the desired camera for each slot cycles through the pattern string.

Łuk chronologiczny: gdy pliki źródłowe mają `creation_time` w metadanych, czas każdego klipu normalizowany jest do [0, 1] w skali dnia. Rank funkcja dostaje dodatkowy składnik `chron_match × 0.20` — klipy z rana trafiają na początku muzyki, wieczorne pod koniec (zachody słońca w finale).

Chronological arc: when source files have `creation_time` metadata, each clip's timestamp is normalised to [0, 1] over the recording day. The rank function gains a `chron_match × 0.20` term — morning clips land at the start of the track, evening clips (sunsets) towards the fade-out.

GPS boost: gdy `gps_weight > 0` i sceny mają kolumny GPS, score CLIP jest modyfikowany już na etapie kroku 5b. Music-driven używa zmodyfikowanego score — sceny z szybką jazdą / ostrymi zakrętami mają wyższy priorytet przy dopasowaniu do slotów.

GPS boost: when `gps_weight > 0` and scenes have GPS columns, CLIP score is modified in step 5b. Music-driven uses the boosted score — fast-riding / sharp-cornering scenes get higher slot priority.

Beaty per ujęcie / Beats per shot: harmonogram cięć budowany przez `build_schedule()` z trzema tierami: fast (`beats_fast` beatów/slot, domyślnie 3), mid (`beats_mid`, domyślnie 4), slow (`beats_slow`, domyślnie 6). Tier przypisywany per muzyczny segment na podstawie energii. Przy 99 BPM: fast ≈ 1.8s/slot, mid ≈ 2.4s/slot, slow ≈ 3.6s/slot.

Beats per shot: the cut schedule is built by `build_schedule()` with three tiers: fast (`beats_fast` beats/slot, default 3), mid (`beats_mid`, default 4), slow (`beats_slow`, default 6). Tier assigned per music segment by energy level. At 99 BPM: fast ≈ 1.8s/slot, mid ≈ 2.4s/slot, slow ≈ 3.6s/slot.

### Dobór i miks muzyki (Traditional mode)

Biblioteka muzyczna analizowana raz i cache'owana w `index.json` (BPM, energia, gatunek). Średni score CLIP mapowany na docelową energię muzyki:

```
energy_target = (avg_score - 0.14) × 10   (obcięte do 0.2–0.9)
```

Materiał wysoko oceniany → energetyczna muzyka. Filtrowanie po czasie trwania (utwór ≈ długość highlight ±5s). Finalny wybór losowany z top 5 kandydatów. Kolejne rundy tworzą nowe pliki `v2.mp4`, `v3.mp4` — poprzednie nie są nadpisywane.

### Prompty CLIP i auto-generowanie

Prompty edytowalne w zakładce **Settings** lub w `config.ini`. Przycisk **Generate CLIP prompts** wywołuje Claude API i generuje prompty POSITIVE/NEGATIVE na podstawie opisu wyjazdu.

### Pliki wyjściowe

```
projekt/
├── 2025-04-Grecja-04.26-md_v1.mp4         ← music-driven wynik
├── 2025-04-Grecja-04.26-md_v2.mp4         ← kolejna muzyka / kolejny render
├── 2025-04-Grecja-04.26_v1.mp4            ← traditional render (gdy używany)
└── _autoframe/
    ├── highlight.mp4                       ← surowy highlight bez intro
    ├── highlight_final.mp4                 ← z intro/outro, bez muzyki
    ├── autocut/                            ← pocięte sceny (-scene-NNN lub -clip-NNN)
    ├── frames/                             ← klatki (_f0/_f1/_f2 = 25/50/75%, JPEG 640px)
    ├── scene_scores.csv                    ← wyniki CLIP (główna kamera)
    ├── scene_scores_allcam.csv             ← wyniki CLIP (wszystkie kamery, gdy score_all_cams)
    ├── selected_scenes.txt                 ← lista do ffmpeg concat
    ├── manual_overrides.json               ← ręczne oznaczenia z Select scenes
    ├── analyze_result.json                 ← cache wyników analizy (threshold, cam_ratio…)
    └── gps_index.json                      ← cache GPS (speed/turn per scena, gdy gps_weight > 0)
```

---

## EN

The pipeline turns a full day of raw footage into a highlight reel without manual editing. Launched from the browser; results available in the Results tab without copying files.

### Pipeline steps — Analyze (shared by both modes)

| Step | Description |
|------|-------------|
| 1 | Find MP4 files in the working directory |
| 2 | Scene cut detection — PySceneDetect `detect-content` **or** CLIP-first (frame scan every N s, CLIP peaks) |
| 3 | Split — each scene as a separate file in `_autoframe/autocut/` (stream copy) |
| 4 | Key frame extraction — 3 frames per clip (_f0/_f1/_f2 = 25/50/75%) → `_autoframe/frames/` |
| 5 | CLIP scoring — `ViT-L-14` on GPU → `scene_scores.csv` / `scene_scores_allcam.csv` |
| 5b | GPS annotation (optional) — exiftool extracts GPS track from Insta360 MP4s; per-scene speed + turn rate added to CSV; blended into CLIP score when `gps_weight > 0` |

Steps 1–5 results are cached — rerunning skips already-processed stages.

### Pipeline steps — ♪ Music-driven (default mode)

| Step | Description |
|------|-------------|
| 6 | Music analysis — librosa: beats + energy envelope → cut schedule synced to rhythm; slot length = beats_fast/mid/slow beats per segment |
| 7 | Motion analysis — OpenCV frame diff for top-N clips by CLIP score |
| 8 | Clip matching — rank: CLIP×0.50 + motion×0.30 + chronological arc×0.20; camera order from `cam_pattern` |
| 9 | Trim + encode each clip (NVENC) → concat → music mix |
| 10 | Intro (top CLIP-scored frame, full resolution) + outro + fade |
| 11 | Output: `YYYY-MM-Place-DD-md_v1.mp4` — subsequent runs `md_v2`, `md_v3…` |

### Pipeline steps — ▶ Render Highlight (Traditional mode)

| Step | Description |
|------|-------------|
| 6 | Scene selection — CLIP threshold, manual overrides, camera balancing, per-file cap |
| 7 | Trim + encode selected scenes → concat → `_autoframe/highlight.mp4` |
| 8 | Intro + outro + fade → `_autoframe/highlight_final.mp4` |
| 9 | Music selection by BPM/energy, mix → `YYYY-MM-Place-DD_v1.mp4` |

### Scene detection — CLIP-first (default)

The default method. Does not look for edit cuts — looks for good content. Scans frames every N seconds (default 3s), scores each with CLIP, detects local maxima (peaks), and extracts a clip (default 8s) around each peak. Minimum gap between clips (default 30s) prevents overlap.

Output: `-clip-NNN` files in `autocut/`. Required for music-driven multicam — `score_all_cams` scores all cameras → `scene_scores_allcam.csv`.

### Scene detection — PySceneDetect (optional)

Available when CLIP-first is disabled in the Advanced modal. Detects cuts via frame-to-frame colour histogram differences (`detect-content`). ffprobe detects real fps and passes it via `--frame-rate` — fixes ~10x timecode errors in VFR files (common in back-cam footage with non-standard `time_base`).

Parameters: `threshold` (default 20), `min_scene_len` (default 8s). Output: `-scene-NNN` files in `autocut/`.

Detection results cached per file — changing parameters and Re-analyzing only reprocesses files without a cached CSV.

### CLIP scoring

OpenCLIP `ViT-L-14` (OpenAI weights) on GPU, processing frames in batches (default 64).

```
final_score = pos_score - neg_score × neg_weight
```

Results written to `scene_scores.csv` (main camera) or `scene_scores_allcam.csv` (all cameras).

### Scene selection (Traditional mode only)

Scenes filtered and selected per source file:

- Only scenes above `threshold` (set in Select scenes).
- Each file has a `max_per_file_sec` cap.
- Each scene trimmed to `max_scene_sec`, centred on the midpoint.
- Clips shorter than `min_take_sec` after trimming are discarded.
- Manual overrides from Select scenes (force-include / force-exclude) take precedence.

### Dual-camera (multicam)

When two cameras are configured (e.g. helmet + rear):

- Camera A (helmet, AUDIO_CAM) is CLIP-scored and drives selection.
- Camera B (rear) is not scored — scenes matched by timestamp proximity (±30s).
- Timestamps computed from PySceneDetect CSV `Start Time (seconds)` + ffprobe fps (fixes ~10x VFR error).
- Selected pairs interleaved: `helmet[1] → back[1] → helmet[2] → back[2] → …`
- Camera B is muted; audio comes from Camera A only.

When `score_all_cams=true` (auto-enabled with CLIP-first): all cameras are CLIP-scored → `scene_scores_allcam.csv`. Music-driven uses the allcam CSV when present.

Select scenes duration estimate accounts for `cam_ratio` (total scenes / main-cam scenes) — accurate even before render via background dry-run API.

### Encoding

Selected scenes are trimmed and re-encoded to a common format (libx264, aac 48kHz stereo, CFR) before the final concat. Audio re-encoding eliminates A/V sync glitches at camera transitions (VFR source → CFR output). Final encoding: 4K upscale (Lanczos), 60fps CFR, NVENC if available.

4K is intentional — YouTube allocates significantly more bitrate to 4K uploads than 1080p.

### Intro/outro

Best-scoring frame as background, two-line Caveat Bold title (year + trip name from directory), configurable outro card, fade in/out, assembled via stream copy.

### Music-driven render (default mode)

Clips are matched to the music structure rather than assembled in timeline order.

```
load_audio()    → librosa beat tracking + segment detection
match_clips()   → fill each segment with highest-scoring available clips
render()        → ffmpeg concat + music mix + intro/outro
```

Source diversity: a rolling `recent_sources` window prevents visual repetition from the same source file. Each clip used at most once.

Camera diversity: `recent_cameras` deque (maxlen=1) alternates cameras on every cut. Falls back gracefully when one camera's clips are exhausted.

Chronological arc: source file `creation_time` → normalised day timeline [0, 1]. Morning footage opens the edit, evening footage closes it. Weight 0.20 in rank function (score×0.50 + motion×0.30 + chron×0.20). Disabled automatically when metadata is absent.

Output: `*-md_v1.mp4`, subsequent runs `md_v2`, `md_v3…`

### Music selection (Traditional mode)

Library analysed once and cached in `index.json` (BPM, energy, genre). Average CLIP score mapped to energy target:

```
energy_target = (avg_score - 0.14) × 10   (clamped 0.2–0.9)
```

High-scoring footage → energetic music. Filtered by duration (track ≈ highlight length ±5s). Final pick chosen randomly from top 5 — ensures variety across runs. Each music rerun creates a new versioned file.

### Output files

```
project/
├── 2025-04-Grecja-04.26-md_v1.mp4        ← music-driven output
├── 2025-04-Grecja-04.26-md_v2.mp4        ← next music-driven run
├── 2025-04-Grecja-04.26_v1.mp4           ← traditional render (when used)
└── _autoframe/
    ├── highlight.mp4                      ← raw highlight without intro
    ├── highlight_final.mp4                ← with intro/outro, no music
    ├── autocut/                           ← split scenes (-scene-NNN or -clip-NNN)
    ├── frames/                            ← key frames (_f0/_f1/_f2 = 25/50/75%, JPEG)
    ├── scene_scores.csv                   ← CLIP scores (main camera)
    ├── scene_scores_allcam.csv            ← CLIP scores (all cameras, when score_all_cams)
    ├── selected_scenes.txt                ← ffmpeg concat list
    ├── manual_overrides.json              ← Select scenes overrides
    ├── analyze_result.json               ← analysis cache (threshold, cam_ratio…)
    └── gps_index.json                    ← GPS cache (speed/turn per scene, when gps_weight > 0)
```
