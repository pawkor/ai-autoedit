# Jak to działa / How it works

## PL

Pipeline zamienia surowy materiał z całego dnia w highlight reel bez ręcznego montażu. Uruchamiany przez przeglądarkę, wyniki dostępne w zakładce Results bez kopiowania plików.

### Kroki pipeline

| Krok | Opis |
|------|------|
| 1 | Znalezienie plików MP4 w katalogu roboczym |
| 2 | Detekcja cięć — PySceneDetect `detect-content` z fps wykrytym przez ffprobe |
| 3 | Podział — każda scena jako osobny plik w `_autoframe/autocut/` (stream copy) |
| 4 | Ekstrakcja klatki środkowej dla każdego klipu → `_autoframe/frames/` (640px) |
| 5 | Scoring CLIP — `ViT-L-14` na GPU, wyniki w `_autoframe/scene_scores.csv` |
| 6 | Selekcja + manualne overrides + przycinanie + enkodowanie scen |
| 7 | Concat → `_autoframe/highlight.mp4` |
| 8 | Intro (klatka z najwyższym score) + outro + fade → `_autoframe/highlight_final.mp4` |
| 9 | Dobór muzyki, miks → `highlight_final_music_v1.mp4` (kolejne rundy: v2, v3…) |

Wyniki kroków 1–5 są cache'owane — ponowne uruchomienie (np. po zmianie threshold) pomija już przetworzone etapy.

### Detekcja scen

Każdy plik MP4 przechodzi przez algorytm `detect-content` PySceneDetect — różnice histogramów kolorów między klatkami. Przed uruchomieniem ffprobe wykrywa rzeczywisty fps pliku i przekazuje go do scenedetect przez `--frame-rate` — rozwiązuje problem błędnych timecode'ów w plikach VFR (np. kamery tylne z niestandardowym `time_base`).

Parametry sterowane z UI (Settings → Scene detection):
- `threshold` (domyślnie 20) — czułość detektora, zakres typowy 20–35
- `min_scene_len` (domyślnie 8s) — minimalna długość sceny

Wyniki detekcji są cache'owane per plik — zmiana parametrów i Re-analyze przetwarza tylko pliki bez CSV.

### Scoring CLIP

Model `ViT-L-14` OpenCLIP (wagi OpenAI) na GPU. Klatki przetwarzane w paczkach (domyślnie 64). Dla każdej klatki:

```
pos_score   = średnie podobieństwo cosinusowe do wszystkich promptów pozytywnych
neg_score   = średnie podobieństwo cosinusowe do wszystkich promptów negatywnych
final_score = pos_score - neg_score × neg_weight
```

Wyniki trafiają do `_autoframe/scene_scores.csv`.

### Selekcja scen

Sceny filtrowane i wybierane osobno dla każdego pliku źródłowego:

- Tylko sceny powyżej `threshold` (ustawiany w Gallery).
- Każdy plik ma limit `max_per_file_sec`.
- Każda scena przycinana do `max_scene_sec`, wyśrodkowana na środku klipu.
- Klipy krótsze niż `min_take_sec` po przycięciu odrzucane.
- Manualne overrides z Gallery (force-include / force-exclude) mają pierwszeństwo.

### Dual-camera (multicam)

Gdy skonfigurowane są dwie kamery (np. kask + tył motocykla):

- Kamera A (kask, AUDIO_CAM) jest scorowana przez CLIP i stanowi podstawę selekcji.
- Kamera B (tył) nie jest scorowana — sceny dobierane przez timestamp matching (±30s).
- Timestamps obliczane z `Start Time (seconds)` w CSV PySceneDetect + fps z ffprobe (naprawia błąd ~10x w plikach VFR).
- Wybrane pary przeplatane: `helmet[1] → back[1] → helmet[2] → back[2] → …`
- Kamera B jest wyciszana; audio pochodzi wyłącznie z kamery A.

Szacowany czas w Gallery uwzględnia `cam_ratio` (stosunek łącznych scen do scen z głównej kamery) — estymacja jest dokładna nawet przed renderem dzięki background dry-run API.

### Enkodowanie

Wybrane sceny przycinane i re-encodowane do wspólnego formatu (libx264, aac 48kHz stereo, CFR) przed finalnym concat. Re-encoding audio eliminuje desynce na przejściach między kamerami (VFR source → CFR output). Finalne enkodowanie: skalowanie do 4K (Lanczos), 60fps CFR, NVENC jeśli dostępny.

4K jest celowe — YouTube przydziela znacznie więcej bitrate do uploadów 4K niż 1080p.

### Intro i outro

Tło intro: klatka z najwyższym score CLIP. Nad nią dwie linie fontem Caveat Bold: rok + nazwa trasy (auto z nazwy katalogu roboczego). Outro: czarna plansza z konfigurowalnym tekstem. Fade in/out. Montaż przez stream copy do `_autoframe/highlight_final.mp4`.

### Dobór i miks muzyki

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
├── highlight_final_music_v1.mp4   ← główny wynik
├── highlight_final_music_v2.mp4   ← kolejna muzyka
└── _autoframe/
    ├── highlight.mp4              ← surowy highlight bez intro
    ├── highlight_final.mp4        ← z intro/outro, bez muzyki
    ├── autocut/                   ← pocięte sceny
    ├── frames/                    ← klatki środkowe (JPEG 640px)
    ├── scene_scores.csv           ← wyniki CLIP
    ├── selected_scenes.txt        ← lista do ffmpeg concat
    ├── manual_overrides.json      ← ręczne oznaczenia z Gallery
    └── analyze_result.json        ← cache wyników analizy (threshold, cam_ratio…)
```

---

## EN

The pipeline turns a full day of raw footage into a highlight reel without manual editing. Launched from the browser; results available in the Results tab without copying files.

### Pipeline steps

| Step | Description |
|------|-------------|
| 1 | Find MP4 files in the working directory |
| 2 | Scene cut detection — PySceneDetect `detect-content` with ffprobe-detected fps |
| 3 | Split — each scene as a separate file in `_autoframe/autocut/` (stream copy) |
| 4 | Key frame extraction (midpoint JPEG, 640px) → `_autoframe/frames/` |
| 5 | CLIP scoring — `ViT-L-14` on GPU, results in `_autoframe/scene_scores.csv` |
| 6 | Selection + manual overrides + trimming + scene encoding |
| 7 | Concat → `_autoframe/highlight.mp4` |
| 8 | Intro (top-scoring frame) + outro + fade → `_autoframe/highlight_final.mp4` |
| 9 | Music selection, mix → `highlight_final_music_v1.mp4` (subsequent runs: v2, v3…) |

Steps 1–5 results are cached — rerunning after a threshold change skips already-processed stages.

### Scene detection

PySceneDetect `detect-content` — frame-to-frame colour histogram differences. ffprobe detects the real fps before each run and passes it via `--frame-rate` — fixes ~10x timecode errors in VFR files (common in back-cam footage with non-standard `time_base`).

Parameters controlled from UI (Settings → Scene detection):
- `threshold` (default 20) — detector sensitivity, typical range 20–35
- `min_scene_len` (default 8s) — minimum scene duration

Detection results cached per file — changing parameters and Re-analyzing only reprocesses files without a cached CSV.

### CLIP scoring

OpenCLIP `ViT-L-14` (OpenAI weights) on GPU, processing frames in batches (default 64).

```
final_score = pos_score - neg_score × neg_weight
```

### Scene selection

Scenes filtered and selected per source file:

- Only scenes above `threshold` (set in Gallery).
- Each file has a `max_per_file_sec` cap.
- Each scene trimmed to `max_scene_sec`, centred on the midpoint.
- Clips shorter than `min_take_sec` after trimming are discarded.
- Manual overrides from Gallery (force-include / force-exclude) take precedence.

### Dual-camera (multicam)

When two cameras are configured (e.g. helmet + rear):

- Camera A (helmet, AUDIO_CAM) is CLIP-scored and drives selection.
- Camera B (rear) is not scored — scenes matched by timestamp proximity (±30s).
- Timestamps computed from PySceneDetect CSV `Start Time (seconds)` + ffprobe fps (fixes ~10x VFR error).
- Selected pairs interleaved: `helmet[1] → back[1] → helmet[2] → back[2] → …`
- Camera B is muted; audio comes from Camera A only.

Gallery duration estimate accounts for `cam_ratio` (total scenes / main-cam scenes) — accurate even before render via background dry-run API.

### Encoding

Selected scenes are trimmed and re-encoded to a common format (libx264, aac 48kHz stereo, CFR) before the final concat. Audio re-encoding eliminates A/V sync glitches at camera transitions (VFR source → CFR output). Final encoding: 4K upscale (Lanczos), 60fps CFR, NVENC if available.

4K is intentional — YouTube allocates significantly more bitrate to 4K uploads than 1080p.

### Intro/outro

Best-scoring frame as background, two-line Caveat Bold title (year + trip name from directory), configurable outro card, fade in/out, assembled via stream copy.

### Music selection

Library analysed once and cached in `index.json` (BPM, energy, genre). Average CLIP score mapped to energy target:

```
energy_target = (avg_score - 0.14) × 10   (clamped 0.2–0.9)
```

High-scoring footage → energetic music. Filtered by duration (track ≈ highlight length ±5s). Final pick chosen randomly from top 5 — ensures variety across runs. Each music rerun creates a new versioned file.

### Output files

```
project/
├── highlight_final_music_v1.mp4   ← main output
├── highlight_final_music_v2.mp4   ← next music run
└── _autoframe/
    ├── highlight.mp4              ← raw highlight without intro
    ├── highlight_final.mp4        ← with intro/outro, no music
    ├── autocut/                   ← split scenes
    ├── frames/                    ← midpoint frames (JPEG 640px)
    ├── scene_scores.csv           ← CLIP scores
    ├── selected_scenes.txt        ← ffmpeg concat list
    ├── manual_overrides.json      ← Gallery overrides
    └── analyze_result.json        ← analysis cache (threshold, cam_ratio…)
```
