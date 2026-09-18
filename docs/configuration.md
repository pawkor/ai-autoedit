# Konfiguracja / Configuration

## PL

Konfiguracja dwupoziomowa: globalny `config.ini` w katalogu repo + opcjonalny `config.ini` w katalogu projektu (ma pierwszeństwo). Przez Web UI: zakładka **Settings** zapisuje zmiany do `config.ini` projektu automatycznie po opuszczeniu każdego pola.

### `[scene_detection]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `threshold` | `20` | Czułość detektora cięć (PySceneDetect). Wyższy = mniej cięć. Zakres typowy: 20–35. Dla leśnego materiału z prześwitami słońca zalecane 24–28. |
| `min_scene_len` | `8` | Minimalna długość wykrytej sceny w sekundach. |

> Zmiana tych parametrów wymaga **Re-analyze** — są stosowane przy detekcji, nie selekcji.

### `[clip_prompts]`

```ini
[clip_prompts]
positive =
    scenic motorcycle road trip through mountains
    winding mountain pass with beautiful surroundings

negative =
    boring flat highway with no scenery
    parking lot or gas station
```

Każdy prompt w osobnej linii (wcięcie = kontynuacja wartości INI). Im bardziej specyficzny prompt, tym lepiej model rozróżnia dobry materiał.

> **Prompty powinny opisywać to co faktycznie jest w materiale.** Nieistotne prompty rozmywają score — nie dodawaj lokalizacji których nie ma w nagraniach.

Prompty generowane automatycznie przez Claude — opisz dzień jazdy w formularzu **New project** lub w zakładce **Settings**, kliknij **Generate CLIP prompts**.

### `[clip_scoring]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `model` | `ViT-L-14` | Backbone OpenCLIP dla trybu Traditional (`clip_score.py`) i mood scoringu legacy. `ViT-L-14`/`openai` pasuje do predyktora aesthetic bez drugiego passa. |
| `pretrained` | `openai` | Tag wag OpenCLIP dla powyższego modelu. |
| `top_percent` | `25` | Procent najlepszych scen drukowanych w logu. Tylko wyświetlanie. |
| `neg_weight` | `0.5` | Waga negatywnych promptów: `final = pos - neg × neg_weight`. Dla ciemnego materiału obniż do `0.3`. |
| `batch_size` | `64` | Klatek przez GPU jednocześnie. Obniż do `32`/`16` przy błędach OOM. |
| `clip_workers` | `4` | Wątki ładowania klatek dla dataloadera CLIP. |

### `[clip_scan]`

Backbone trybu CLIP-first (`clip_scan.py`) — niezależny od `[clip_scoring]`.

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `model` | `ViT-SO400M-16-SigLIP2-384` | Model OpenCLIP do skanowania klatek. Przy niepowodzeniu ładowania kod cofa się do `ViT-H-14`/`dfn5b`. |
| `pretrained` | `webli` | Tag wag. Prefiks `hf-hub:` pozwala użyć modelu spoza rejestru OpenCLIP. |

Zmiana modelu (lub brak zapisanego hasha modelu) inwaliduje cały cache skanu — `frame_raw_scores/` i `selected_peaks/` — i wymusza pełny rescan GPU. Faktycznie załadowany model jest zapisywany w `scene_embeddings.npz` (metadane `model`/`pretrained`), skąd czyta go `mood_score.py`; predyktor aesthetic przy backbone innym niż `ViT-L-14`/`openai` wykonuje osobny mały pass ViT-L-14 tylko na klatkach peaków.

### `[scene_selection]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `threshold` | `0.148` | Minimalny score CLIP. Ustawiany automatycznie przez wyszukiwanie binarne (Target dur.) w zakładce Select scenes. |
| `max_scene_sec` | `10` | Maks. sekund z jednego klipu. Wycięty do środka. |
| `max_per_file_sec` | `45` | Maks. łącznych sekund z jednego pliku źródłowego. Sceny przekraczające ten limit widoczne w Select scenes z oznaczeniem „limit". |
| `min_take_sec` | `0.5` | Klipy krótsze od tej wartości po przycięciu są odrzucane. |
| `gps_weight` | `0.0` | Waga GPS boost (0–1). Gdy > 0, score CLIP mnożony przez `(1 + gps_weight × (speed_norm×0.7 + turn_norm×0.3))`. Wymaga danych GPS w plikach MP4 (Insta360). |

### `[music_driven]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `beats_fast` | `3` | Beaty na ujęcie w szybkich segmentach (chorus, peak energy). |
| `beats_mid` | `4` | Beaty na ujęcie w średnich segmentach. |
| `beats_slow` | `6` | Beaty na ujęcie w wolnych segmentach (intro, outro). |
| `cam_pattern` | *(puste)* | Wzorzec kolejności kamer — np. `aabaab`. Litera `a` = Cam A, `b` = Cam B. Puste = score-driven alternation. |

### `[video]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `resolution` | `3840:2160` | Rozdzielczość wyjściowa. 4K = więcej bitrate na YouTube. |
| `framerate` | `60` | Klatkaż wyjściowy. |
| `audio_bitrate` | `192k` | Bitrate audio. |
| `nvenc_cq` | `18` | Stała jakość NVENC (niższy = lepsza jakość). |
| `nvenc_preset` | `p5` | Preset NVENC. `p1`=najszybszy, `p7`=najlepsza jakość. |
| `x264_crf` | `15` | Jakość CRF libx264 (fallback bez GPU). |
| `x264_preset` | `fast` | Preset libx264. |

### `[intro_outro]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `duration` | `3` | Czas trwania plansz intro i outro (sekundy). |
| `fade_duration` | `1` | Czas fade in/out. |
| `outro_text` | `Editing powered by AI` | Tekst planszy outro. |
| `font` | `~/fonts/Caveat-Bold.ttf` | Plik TTF/OTF dla drawtext. |
| `font_size_title` | `120` | Rozmiar fontu — pierwsza linia intro. |
| `font_size_subtitle` | `96` | Rozmiar fontu — druga linia intro. |
| `font_size_outro` | `60` | Rozmiar fontu — outro. |

### `[music]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `dir` | `/data/music` | Katalog z plikami MP3/M4A (ścieżka w kontenerze). |
| `music_volume` | `0.7` | Głośność muzyki w finalnym miksie (0–1). |
| `original_volume` | `0.3` | Głośność oryginalnego audio. `0` = całkowite wyciszenie. |
| `fade_out_duration` | `3` | Czas wygaszania muzyki na końcu (sekundy). |

### `[paths]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `work_subdir` | `_autoframe` | Podkatalog roboczy tworzony w katalogu każdego projektu. |
| `ffmpeg` | `/usr/lib/jellyfin-ffmpeg/ffmpeg` | Ścieżka do ffmpeg w kontenerze. |
| `ffprobe` | `/usr/lib/jellyfin-ffmpeg/ffprobe` | Ścieżka do ffprobe w kontenerze. |

### Przykład per-project config.ini

```ini
[scene_detection]
threshold = 26
min_scene_len = 10

[clip_prompts]
positive =
    motorcycle riding through narrow gorge canyon limestone rock walls
    road at bottom of deep canyon vertical cliffs both sides

negative =
    boring flat highway with no scenery
    parking lot or gas station

[clip_scoring]
neg_weight = 0.3

[scene_selection]
threshold = 0.138
max_per_file_sec = 30
max_scene_sec = 5
```

---

## EN

Two-level configuration: global `config.ini` in the repo root + optional per-project `config.ini` (takes precedence). Via Web UI: **Settings** tab writes changes to the project's `config.ini` automatically on field blur.

### `[scene_detection]`

| Key | Default | Description |
|-----|---------|-------------|
| `threshold` | `20` | Scene cut detector sensitivity. Higher = fewer cuts. Typical range 20–35. For forest/lighting-heavy footage try 24–28. |
| `min_scene_len` | `8` | Minimum detected scene duration in seconds. |

> Changing these requires **Re-analyze** — applied during detection, not selection.

### `[clip_prompts]`

Each prompt on its own line (indentation = INI value continuation). More specific prompts = better discrimination.

> **Prompts should describe what is actually in the footage.** Irrelevant prompts dilute the score.

Auto-generated by Claude — describe the ride in the **New project** form or **Settings** tab, click **Generate CLIP prompts**.

### `[clip_scoring]`

| Key | Default | Description |
|-----|---------|-------------|
| `model` | `ViT-L-14` | OpenCLIP backbone for Traditional mode (`clip_score.py`) and legacy mood scoring. `ViT-L-14`/`openai` matches the aesthetic predictor without a second pass. |
| `pretrained` | `openai` | OpenCLIP weights tag for the model above. |
| `top_percent` | `25` | Top scenes printed in summary log. Display only. |
| `neg_weight` | `0.5` | `final = pos - neg × neg_weight`. Lower to `0.3` for dark footage. |
| `batch_size` | `64` | Frames per GPU batch. Lower to `32`/`16` if OOM. |
| `clip_workers` | `4` | Dataloader worker threads for CLIP frame loading. |

### `[clip_scan]`

CLIP-first mode backbone (`clip_scan.py`) — independent from `[clip_scoring]`.

| Key | Default | Description |
|-----|---------|-------------|
| `model` | `ViT-SO400M-16-SigLIP2-384` | OpenCLIP model for frame scanning. Falls back to `ViT-H-14`/`dfn5b` if loading fails. |
| `pretrained` | `webli` | Weights tag. `hf-hub:` prefix allows models outside the OpenCLIP registry. |

Changing the model (or a missing model hash) invalidates the whole scan cache — `frame_raw_scores/` and `selected_peaks/` — and forces a full GPU rescan. The actually-loaded model is recorded in `scene_embeddings.npz` (`model`/`pretrained` metadata) and read back by `mood_score.py`; with any backbone other than `ViT-L-14`/`openai` the aesthetic predictor runs a small dedicated ViT-L-14 pass on peak frames only.

### `[scene_selection]`

| Key | Default | Description |
|-----|---------|-------------|
| `threshold` | `0.148` | Minimum CLIP score. Set automatically by the binary search (Target dur.) in the Select scenes tab. |
| `max_scene_sec` | `10` | Max seconds per scene. Trimmed to midpoint. |
| `max_per_file_sec` | `45` | Max total seconds from one source file. Scenes exceeding this cap shown in Select scenes with "limit" badge. |
| `min_take_sec` | `0.5` | Clips shorter than this after trimming are discarded. |
| `gps_weight` | `0.0` | GPS boost weight (0–1). When > 0, CLIP score multiplied by `(1 + gps_weight × (speed_norm×0.7 + turn_norm×0.3))`. Requires GPS data in MP4 files (Insta360). |

### `[music_driven]`

| Key | Default | Description |
|-----|---------|-------------|
| `beats_fast` | `3` | Beats per shot in fast segments (chorus, peak energy). |
| `beats_mid` | `4` | Beats per shot in mid segments. |
| `beats_slow` | `6` | Beats per shot in slow segments (intro, outro). |
| `cam_pattern` | *(empty)* | Camera order pattern — e.g. `aabaab`. Letter `a` = Cam A, `b` = Cam B from Settings. Empty = score-driven alternation. |

### `[video]`

| Key | Default | Description |
|-----|---------|-------------|
| `resolution` | `3840:2160` | Output resolution. 4K = more YouTube bitrate. |
| `framerate` | `60` | Output framerate. |
| `audio_bitrate` | `192k` | Audio bitrate. |
| `nvenc_cq` | `18` | NVENC constant quality (lower = better). |
| `nvenc_preset` | `p5` | NVENC preset. `p1`=fastest, `p7`=best quality. |
| `x264_crf` | `15` | libx264 CRF (GPU fallback). |
| `x264_preset` | `fast` | libx264 speed preset. |

### `[intro_outro]`

| Key | Default | Description |
|-----|---------|-------------|
| `duration` | `3` | Intro/outro card duration (seconds). |
| `fade_duration` | `1` | Fade in/out duration. |
| `outro_text` | `Editing powered by AI` | Outro card text. |
| `font` | `~/fonts/Caveat-Bold.ttf` | TTF/OTF font for drawtext. |
| `font_size_title` | `120` | Font size — intro first line. |
| `font_size_subtitle` | `96` | Font size — intro second line. |
| `font_size_outro` | `60` | Font size — outro. |

### `[music]`

| Key | Default | Description |
|-----|---------|-------------|
| `dir` | `/data/music` | MP3/M4A directory (container path). |
| `music_volume` | `0.7` | Music volume in final mix (0–1). |
| `original_volume` | `0.3` | Original audio volume. `0` = silence. |
| `fade_out_duration` | `3` | Music fade-out duration (seconds). |

### `[paths]`

| Key | Default | Description |
|-----|---------|-------------|
| `work_subdir` | `_autoframe` | Working subdirectory created inside each project folder. |
| `ffmpeg` | `/usr/lib/jellyfin-ffmpeg/ffmpeg` | Path to ffmpeg inside the container. |
| `ffprobe` | `/usr/lib/jellyfin-ffmpeg/ffprobe` | Path to ffprobe inside the container. |
