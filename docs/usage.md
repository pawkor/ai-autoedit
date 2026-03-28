# Użycie / Usage

## PL

### Uruchomienie

```bash
cd ~/moto/2025/08-Rumunia/12
source ~/highlight-env/bin/activate
~/ai-autoedit/autoframe.sh [opcje]
```

### Opcje

```
--threshold N         Próg score CLIP (domyślnie: 0.148)
--max-scene N         Maks. sekund z jednego klipu sceny (domyślnie: 10)
--per-file N          Maks. sekund z jednego pliku źródłowego (domyślnie: 45)
--title "TEXT"        Tekst planszy intro, \n = nowa linia (domyślnie: auto z nazwy katalogu)
--font /path/to.ttf   Plik fontu (domyślnie: ~/fonts/Caveat-Bold.ttf)
--no-intro            Pomiń intro/outro
--cam-a DIR           Podfolder kamery A — źródło audio, np. "helmet"
--cam-b DIR           Podfolder kamery B — wyciszona, np. "360"
--music /path         Katalog z plikami MP3/M4A (domyślnie: ~/moto/music)
--music-volume N      Głośność muzyki 0–1 (domyślnie: 0.7)
--original-volume N   Głośność oryginalnego audio 0–1 (domyślnie: 0.3)
--music-genre GENRE   Filtr gatunku, np. "house", "rock", "dnb"
--music-artist NAME   Filtr artysty, np. "elektronomia" lub "tobu,alan,janji"
--music-rebuild       Przebuduj indeks muzyczny i wyjdź
--no-music            Pomiń miks muzyczny
--gpudetect           Detekcja scen GPU przez decord (domyślnie: CPU scenedetect)
--about "TEXT"        Opis dnia — auto-generuje config.ini przez Claude API (Haiku)
--reframe             Wymuś reprojekcję 360° nawet bez auto-wykrytych plików .insv
--help                Wyświetl pomoc
```

### Przykłady

```bash
# Podstawowe uruchomienie
~/ai-autoedit/autoframe.sh --threshold 0.15

# Auto-generowanie config.ini opisem dnia (Claude Haiku API)
~/ai-autoedit/autoframe.sh --about "Transfăgărășan, górskie serpentyny, słoneczna pogoda, dramatyczne widoki"

# Dwie kamery: kamera przednia + kamera 360° Insta360 X2
~/ai-autoedit/autoframe.sh --cam-a helmet --cam-b 360

# GPU detekcja scen
~/ai-autoedit/autoframe.sh --gpudetect

# Konkretna muzyka
~/ai-autoedit/autoframe.sh --music-genre "progressive house" --music-artist "elektronomia"
~/ai-autoedit/autoframe.sh --music-artist "tobu,janji,alan"

# Bez intro, bez muzyki
~/ai-autoedit/autoframe.sh --no-intro --no-music

# Własny tytuł
~/ai-autoedit/autoframe.sh --title "Transalpina\nSierpień 2025"
```

### Struktura katalogów

```
~/moto/2025/08-Rumunia/12/   ← CWD przy uruchomieniu
├── *.mp4                     ← pliki źródłowe (lub w podfolderach)
├── helmet/                   ← opcjonalnie: cam-a
├── 360/                      ← opcjonalnie: cam-b
│   ├── LRV_*.insv            ← proxy 360° (do detekcji i scoringu)
│   └── VID_*.insv            ← high-res 360° (do finalnego renderu)
├── config.ini                ← opcjonalnie: nadpisania per event
└── _autoframe/               ← katalog roboczy (tworzony automatycznie)
    ├── autocut/              ← podzielone sceny
    ├── frames/               ← klatki kluczowe dla CLIP
    ├── csv/                  ← pliki CSV detekcji scen
    ├── trimmed/              ← przycięte/wyciszone klipy
    ├── reframed/             ← klipy LRV po reprojekcji v360
    ├── vid_trimmed/          ← klipy VID_ high-res (proxy reframe)
    └── scene_scores.csv
```

Wyniki pipeline:
```
highlight.mp4               ← surowy highlight
highlight_final.mp4         ← z intro/outro
highlight_final_music.mp4   ← finalny z muzyką
```

> **Uwaga:** Nazwy plików MP4 muszą być małymi literami.
> ```bash
> rename 'y/A-Z/a-z/' *.MP4
> ```

### Częściowy re-render (z użyciem cache)

```bash
# Tylko nowy miks muzyczny
rm highlight_final_music.mp4

# Re-render intro/outro
rm _autoframe/intro.mp4 _autoframe/outro.mp4 _autoframe/highlight_faded.mp4 highlight_final.mp4

# Od selekcji scen wzwyż
rm _autoframe/selected_scenes.txt highlight.mp4 highlight_final.mp4 highlight_final_music.mp4

# Wymuś pełne przeliczenie scoringu (zmiana promptów lub neg_weight)
rm _autoframe/scene_scores.csv

# Re-reframe LRV 360° (zmiana kąta w config.ini)
rm -rf _autoframe/reframed/

# Re-render proxy VID_ (zmiana kąta lub vid_input_format)
rm -rf _autoframe/vid_trimmed/
```

### Wykluczanie scen

Żeby trwale wykluczyć scenę, ustaw jej score na 0 w `_autoframe/scene_scores.csv`:

```bash
# Wyklucz konkretną scenę
awk -F',' -v OFS=',' '$1=="PLIK-scene-014"{$2=0; $3=0; $4=0} 1' \
    _autoframe/scene_scores.csv > /tmp/tmp.csv && mv /tmp/tmp.csv _autoframe/scene_scores.csv

# Wyklucz wszystkie sceny z danego pliku
awk -F',' -v OFS=',' '/^PLIK/{$2=0; $3=0; $4=0} 1' \
    _autoframe/scene_scores.csv > /tmp/tmp.csv && mv /tmp/tmp.csv _autoframe/scene_scores.csv
```

Następnie usuń `_autoframe/selected_scenes.txt` i `highlight*.mp4` żeby wymusić re-render.

---

## EN

### Run

```bash
cd ~/moto/2025/08-Romania/12
source ~/highlight-env/bin/activate
~/ai-autoedit/autoframe.sh [options]
```

### Options

```
--threshold N         CLIP score threshold (default: 0.148)
--max-scene N         Max seconds per scene clip (default: 10)
--per-file N          Max seconds per source file (default: 45)
--title "TEXT"        Intro title text, \n = new line (default: auto from directory name)
--font /path/to.ttf   Font file (default: ~/fonts/Caveat-Bold.ttf)
--no-intro            Skip intro/outro
--cam-a DIR           Camera A subfolder — audio source, e.g. "helmet"
--cam-b DIR           Camera B subfolder — muted, e.g. "360"
--music /path         Directory with MP3/M4A files (default: ~/moto/music)
--music-volume N      Music volume 0–1 (default: 0.7)
--original-volume N   Original audio volume 0–1 (default: 0.3)
--music-genre GENRE   Genre filter, e.g. "house", "rock", "dnb"
--music-artist NAME   Artist filter, e.g. "elektronomia" or "tobu,alan,janji"
--music-rebuild       Rebuild music index and exit
--no-music            Skip music mixing
--gpudetect           GPU scene detection via decord (default: CPU scenedetect)
--about "TEXT"        Describe the day's ride — auto-generates config.ini via Claude API
--reframe             Force 360° reframe even without auto-detected .insv files
--help                Show help
```

### Examples

```bash
~/ai-autoedit/autoframe.sh --threshold 0.15
~/ai-autoedit/autoframe.sh --about "Transfăgărășan, mountain hairpins, sunny weather, dramatic views"
~/ai-autoedit/autoframe.sh --cam-a helmet --cam-b 360
~/ai-autoedit/autoframe.sh --gpudetect
~/ai-autoedit/autoframe.sh --music-genre "progressive house" --music-artist "elektronomia"
~/ai-autoedit/autoframe.sh --no-intro --no-music
~/ai-autoedit/autoframe.sh --title "Transalpina\nAugust 2025"
```

### Partial re-render (using cache)

```bash
rm highlight_final_music.mp4                          # re-mix music only
rm _autoframe/scene_scores.csv                        # force full re-score
rm _autoframe/selected_scenes.txt highlight*.mp4      # re-run from selection
rm -rf _autoframe/reframed/                           # re-reframe LRV
rm -rf _autoframe/vid_trimmed/                        # re-render VID_ proxy
```

### Excluding scenes

```bash
# Exclude a specific scene
awk -F',' -v OFS=',' '$1=="FILE-scene-014"{$2=0; $3=0; $4=0} 1' \
    _autoframe/scene_scores.csv > /tmp/tmp.csv && mv /tmp/tmp.csv _autoframe/scene_scores.csv

# Exclude all scenes from a source file
awk -F',' -v OFS=',' '/^FILE/{$2=0; $3=0; $4=0} 1' \
    _autoframe/scene_scores.csv > /tmp/tmp.csv && mv /tmp/tmp.csv _autoframe/scene_scores.csv
```

Then delete `_autoframe/selected_scenes.txt` and `highlight*.mp4` to force a re-render.
