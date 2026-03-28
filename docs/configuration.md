# Konfiguracja / Configuration

## PL

Wszystkie domyślne wartości pipeline są w `<repo>/config.ini`. Flagi CLI mają pierwszeństwo nad wartościami z configa. Per-event `config.ini` w katalogu dnia ma pierwszeństwo nad globalnym — wystarczy podać tylko sekcje i klucze które chcesz zmienić.

### `[scene_detection]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `threshold` | `20` | Czułość detektora cięć (CPU scenedetect). Niższy = więcej cięć. Zakres typowy: 10–40. |
| `min_scene_len` | `8s` | Minimalna długość wykrytej sceny. Sufiks `s` (np. `5s`, `15s`). |
| `gpu_threshold` | `30` | Próg MAD dla `--gpudetect`. Inny zakres niż `threshold` — wymaga osobnej kalibracji. |

### `[clip_prompts]`

```ini
[clip_prompts]
positive =
    scenic motorcycle road trip through mountains
    winding mountain pass Romania Transalpina

negative =
    boring flat highway with no scenery
    parking lot or gas station
```

Każdy prompt w osobnej linii (wcięcie = kontynuacja wartości INI). Im bardziej specyficzny prompt, tym lepiej model rozróżnia dobry materiał.

> **Prompty powinny opisywać to co faktycznie jest w materiale.** Nie dodawaj promptów dla lokalizacji których nie ma w nagraniach — CLIP uśrednia po wszystkich promptach pozytywnych, więc nieistotne rozmywają score.

### `[clip_scoring]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `top_percent` | `25` | Procent najlepszych scen drukowanych w logu. Tylko wyświetlanie. |
| `neg_weight` | `0.5` | Waga negatywnych promptów: `final = pos - neg * neg_weight`. Dla ciemnego materiału obniż do `0.3`. |
| `batch_size` | `64` | Klatek przez GPU jednocześnie. Obniż do `32`/`16` przy błędach OOM. |

### `[scene_selection]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `threshold` | `0.148` | Minimalny score CLIP. Typowy zakres: 0.13–0.16. |
| `max_scene_sec` | `10` | Maks. sekund z jednego klipu. Wycięty do środka. |
| `max_per_file_sec` | `45` | Maks. łącznych sekund z jednego pliku źródłowego. |
| `tier1_cutoff` | `0.145` | Pliki z najlepszą sceną poniżej progu → cap `tier1_limit`. |
| `tier1_limit` | `10` | Cap (sekundy) dla plików tier-1. |
| `tier2_cutoff` | `0.150` | Pliki między `tier1_cutoff` a tym progiem → cap `tier2_limit`. |
| `tier2_limit` | `20` | Cap (sekundy) dla plików tier-2. |
| `min_take_sec` | `0.5` | Klipy krótsze od tej wartości po przycięciu są odrzucane. |

### `[video]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `resolution` | `3840:2160` | Rozdzielczość wyjściowa. 4K = więcej bitrate na YouTube. |
| `framerate` | `60` | Klatkaż wyjściowy. |
| `audio_bitrate` | `192k` | Bitrate audio. |
| `nvenc_cq` | `18` | Stała jakość NVENC (niższy = lepsza jakość). |
| `nvenc_preset` | `p5` | Preset NVENC. `p1`=najszybszy, `p7`=najlepsza jakość. |
| `x264_crf` | `15` | Jakość CRF libx264 (fallback). |
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
| `dir` | `~/moto/music` | Katalog z plikami MP3/M4A. |
| `music_volume` | `0.7` | Głośność muzyki w finalnym miksie (0–1). |
| `original_volume` | `0.3` | Głośność oryginalnego audio. `0` = całkowite wyciszenie. |
| `fade_out_duration` | `3` | Czas wygaszania muzyki na końcu (sekundy). |

### `[reframe]`

Patrz [Kamera 360° i reframe](reframe-360.md).

### `[paths]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `venv` | `~/highlight-env` | Wirtualne środowisko Python. |
| `work_subdir` | `_autoframe` | Podkatalog roboczy w katalogu każdego dnia. |
| `ffmpeg` | `ffmpeg` | Ścieżka do ffmpeg. Ustaw gdy używasz jellyfin-ffmpeg. |
| `ffprobe` | `ffprobe` | Ścieżka do ffprobe. |

### Przykład per-event config.ini

```ini
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
max_per_file_sec = 75
max_scene_sec = 12
min_take_sec = 3
```

---

## EN

All pipeline defaults live in `<repo>/config.ini`. CLI flags take precedence. A per-event `config.ini` in the day folder overrides the global one — only include sections and keys you want to change.

### `[scene_detection]`

| Key | Default | Description |
|-----|---------|-------------|
| `threshold` | `20` | CPU scene cut detector sensitivity. Lower = more cuts. Range typically 10–40. |
| `min_scene_len` | `8s` | Minimum detected scene duration. Use `s` suffix. |
| `gpu_threshold` | `30` | MAD threshold for `--gpudetect`. Different scale — calibrate separately. |

### `[clip_prompts]`

Each prompt on its own line (indentation = INI value continuation). More specific prompts = better discrimination.

> **Prompts should describe what is actually in the footage.** Irrelevant prompts dilute the score.

### `[clip_scoring]`

| Key | Default | Description |
|-----|---------|-------------|
| `top_percent` | `25` | Top scenes printed in summary log. Display only. |
| `neg_weight` | `0.5` | `final = pos - neg * neg_weight`. Lower to `0.3` for dark footage. |
| `batch_size` | `64` | Frames per GPU batch. Lower to `32`/`16` if OOM. |

### `[scene_selection]`

| Key | Default | Description |
|-----|---------|-------------|
| `threshold` | `0.148` | Minimum CLIP score. Typical range: 0.13–0.16. |
| `max_scene_sec` | `10` | Max seconds per scene. Trimmed to midpoint. |
| `max_per_file_sec` | `45` | Max total seconds from one source file. |
| `tier1_cutoff` | `0.145` | Files below this → capped at `tier1_limit`. |
| `tier1_limit` | `10` | Time cap (s) for tier-1 files. |
| `tier2_cutoff` | `0.150` | Files between tier1 and this → capped at `tier2_limit`. |
| `tier2_limit` | `20` | Time cap (s) for tier-2 files. |
| `min_take_sec` | `0.5` | Clips shorter than this after trimming are discarded. |

### `[video]`

| Key | Default | Description |
|-----|---------|-------------|
| `resolution` | `3840:2160` | Output resolution. 4K = more YouTube bitrate. |
| `framerate` | `60` | Output framerate. |
| `audio_bitrate` | `192k` | Audio bitrate. |
| `nvenc_cq` | `18` | NVENC constant quality (lower = better). |
| `nvenc_preset` | `p5` | NVENC preset. `p1`=fastest, `p7`=best quality. |
| `x264_crf` | `15` | libx264 CRF (fallback). |
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
| `dir` | `~/moto/music` | MP3/M4A directory. |
| `music_volume` | `0.7` | Music volume in final mix (0–1). |
| `original_volume` | `0.3` | Original audio volume. `0` = silence. |
| `fade_out_duration` | `3` | Music fade-out duration (seconds). |

### `[reframe]`

See [360° camera and reframe](reframe-360.md).

### `[paths]`

| Key | Default | Description |
|-----|---------|-------------|
| `venv` | `~/highlight-env` | Python virtual environment path. |
| `work_subdir` | `_autoframe` | Working subdirectory inside each day folder. |
| `ffmpeg` | `ffmpeg` | Path to ffmpeg. Set when using jellyfin-ffmpeg. |
| `ffprobe` | `ffprobe` | Path to ffprobe. |
