# Jak to działa / How it works

## PL

Pipeline zamienia surowy materiał z całego dnia w highlight reel bez ręcznego montażu.

**0. Reframe 360° — proxy LRV** (`ffmpeg v360`)

Jeśli `--cam-b` wskazuje na katalog z plikami `LRV_*.insv` (Insta360 X2), pipeline automatycznie reprojekcjonuje je do widoku rectilinear przez filtr `v360` ffmpeg. LRV to low-res proxy (ok. 736×368) już wstępnie złożony do equirectangular przez kamerę — wystarcza jedno przejście reprojekcji. Wynikowe MP4 trafiają do `_autoframe/reframed/` i są traktowane jak zwykłe pliki kamery B w dalszych krokach.

**1. Detekcja scen** (`scenedetect` lub `gpu_detect.py`)

Domyślnie: każdy plik MP4 przechodzi przez algorytm `detect-content` PySceneDetect — różnice histogramów kolorów między klatkami. Gdy różnica przekracza `threshold` (domyślnie 20) przez minimum `min_scene_len` (domyślnie 8s), zapisywana jest granica sceny.

Z `--gpudetect`: klatki dekodowane przez `decord` w porcjach po 128, miniaturyzowane do 64×64, porównywane MAD. Wynik to CSV kompatybilny z PySceneDetect — cache działa tak samo. Jeśli decord nie ma CUDA, automatycznie spada na CPU.

Wyniki detekcji są cache'owane — ponowne uruchomienie pomija przetworzone pliki.

**2. Podział na sceny** (`scenedetect split-video`)

Każda wykryta scena wycinana jako osobny MP4 przez stream copy (bez re-encodingu). Pliki trafiają do `_autoframe/autocut/`.

**3. Ekstrakcja klatek kluczowych** (`ffmpeg`)

Dla każdego klipu większego niż 5 MB wyciągany jest jeden JPEG ze środka klipu (`duration / 2`). Klipy poniżej 5 MB są pomijane. Klatki cache'owane w `_autoframe/frames/`.

**4. Scoring CLIP** (`clip_score.py`, ViT-L-14 na GPU)

Model `ViT-L-14` OpenCLIP (wagi OpenAI) na GPU. Klatki przetwarzane w paczkach (domyślnie 64). Dla każdej klatki:

```
pos_score   = średnie podobieństwo cosinusowe do wszystkich promptów pozytywnych
neg_score   = średnie podobieństwo cosinusowe do wszystkich promptów negatywnych
final_score = pos_score - neg_score × neg_weight
```

Wyniki trafiają do `_autoframe/scene_scores.csv` i są cache'owane.

**5. Selekcja scen** (`select_scenes.py`)

Sceny filtrowane i wybierane osobno dla każdego pliku źródłowego:

- Tylko sceny powyżej `threshold` (domyślnie 0.148).
- Każdy plik ma limit `max_per_file_sec` (domyślnie 45s).
- Słabo oceniane pliki dostają ostrzejszy limit przez system tierów.
- Każda scena przycinana do `max_scene_sec` (domyślnie 10s), wyśrodkowana na środku klipu.
- Klipy krótsze niż `min_take_sec` po przycięciu odrzucane.

W trybie dual-camera sceny z kamery A i B przeplatane chronologicznie. Kamera A jest źródłem audio; kamera B jest wyciszona. Wynik: `_autoframe/selected_scenes.txt`.

**5.5. Proxy reframe VID_ (opcjonalnie)** (`proxy_reframe.py`)

Jeśli `vid_input_format` jest ustawiony w sekcji `[reframe]` config.ini, pipeline zastępuje wybrane klipy LRV odpowiadającymi plikami `VID_` w wysokiej rozdzielczości (2880×2880 dual fisheye). Mapowanie: `LRV_TIMESTAMP_11_NNN` → `VID_TIMESTAMP_10_NNN.insv`. Bez `vid_input_format` krok jest pomijany.

**6. Składanie highlightu** (`ffmpeg`)

Wybrane sceny łączone w `highlight.mp4`. Skalowanie do 4K (Lanczos), normalizacja do 60 fps (CFR), kodowanie NVENC jeśli dostępny, inaczej libx264. 4K jest celowe — YouTube przydziela znacznie więcej bitrate do uploadów 4K.

**7. Intro i outro** (`ffmpeg drawtext`)

Tło intro: klatka z najwyższym score CLIP. Nad nią dwie linie fontem Caveat Bold: rok + nazwa trasy (auto z nazwy katalogu lub `--title`). Outro: czarna plansza z konfigurowalnym tekstem. Fade in/out. Montaż przez stream copy do `highlight_final.mp4`.

**8. Dobór i miks muzyki** (`music_index.py` + `ffmpeg`)

Biblioteka muzyczna analizowana raz i cache'owana w `index.json`. Średni score CLIP mapowany na docelową energię muzyki. Finalny wybór losowany z top 5 kandydatów — różne utwory przy kolejnych uruchomieniach. Wynik: `highlight_final_music.mp4`.

**Auto-generowanie config.ini** (`generate_config.py`)

Flaga `--about "opis dnia"` wywołuje Claude Haiku API przed uruchomieniem pipeline i generuje `config.ini` dopasowany do opisanego materiału. Wymaga zmiennej środowiskowej `ANTHROPIC_API_KEY`.

**Kaskada konfiguracji**

`<repo>/config.ini` przechowuje globalne domyślne. Jeśli w bieżącym katalogu (folder dnia) istnieje `config.ini`, jego wartości mają pierwszeństwo.

---

## Kroki pipeline

| Krok | Opis |
|------|------|
| 0 | Reframe 360° — reprojekcja `LRV_*.insv` → flat MP4 (auto gdy cam-b zawiera .insv) |
| 1 | Znalezienie plików MP4 |
| 2 | Detekcja scen — `scenedetect` (CPU) lub `gpu_detect.py` (GPU) |
| 3 | Podział — każda scena jako osobny plik w `autocut/` |
| 4 | Ekstrakcja klatek kluczowych |
| 5 | Scoring CLIP — `ViT-L-14` na GPU |
| 6 | Selekcja scen |
| 6.5 | Proxy reframe VID_ (opcjonalnie) |
| 7 | Concat → `highlight.mp4` |
| 8 | Intro + outro → `highlight_final.mp4` |
| 9 | Miks muzyczny → `highlight_final_music.mp4` |

Wyniki kroków 0–5 są cache'owane.

---

## EN

**0. 360° reframe — LRV proxy** (`ffmpeg v360`)

If `--cam-b` points to a directory containing `LRV_*.insv` files (Insta360 X2), the pipeline automatically reprojects them to a rectilinear view using ffmpeg's `v360` filter. Output MP4s land in `_autoframe/reframed/`.

**1. Scene detection** (`scenedetect` or `gpu_detect.py`)

Default: PySceneDetect `detect-content` — frame-to-frame colour histogram differences. With `--gpudetect`: frames decoded by `decord` in chunks of 128, downscaled to 64×64, compared with MAD. If decord lacks CUDA, falls back to CPU automatically.

**2. Scene splitting** — stream copy, no re-encoding. Files land in `_autoframe/autocut/`.

**3. Key frame extraction** — midpoint JPEG from each clip >5 MB. Cached in `_autoframe/frames/`.

**4. CLIP scoring** (`clip_score.py`) — OpenCLIP `ViT-L-14` on GPU, batch processing.
```
final_score = pos_score - neg_score × neg_weight
```

**5. Scene selection** (`select_scenes.py`) — threshold filter, tier limits, per-file cap, trim to midpoint. Dual-camera mode interleaves A and B chronologically.

**5.5. VID_ proxy reframe (optional)** — replaces LRV clips with high-res `VID_` files when `vid_input_format` is set in config.ini.

**6. Highlight assembly** — 4K upscale (Lanczos), 60fps CFR, NVENC or libx264.

**7. Intro/outro** — best-scoring frame as background, Caveat Bold title text, fade in/out.

**8. Music mix** — average CLIP score mapped to energy target, random pick from top 5 candidates.

| Step | Description |
|------|-------------|
| 0 | 360° reframe — `LRV_*.insv` → flat MP4 (auto-detected) |
| 1 | Find MP4 files |
| 2 | Scene detection — `scenedetect` (CPU) or `gpu_detect.py` (GPU) |
| 3 | Split scenes → `autocut/` |
| 4 | Key frame extraction |
| 5 | CLIP scoring — `ViT-L-14` on GPU |
| 6 | Scene selection |
| 6.5 | VID_ proxy reframe (optional) |
| 7 | Concat → `highlight.mp4` |
| 8 | Intro + outro → `highlight_final.mp4` |
| 9 | Music mix → `highlight_final_music.mp4` |

Steps 0–5 results are cached.
