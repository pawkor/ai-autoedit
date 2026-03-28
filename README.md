# autoframe — AI Highlight Reel Pipeline

Każdy dzień na motocyklu to kilkaset gigabajtów surowego materiału z jednej lub dwóch kamer. Ręczny montaż takiej ilości nagrań — przeglądanie, wycinanie, składanie — zajmuje wielokrotnie więcej czasu niż sam wyjazd. Po powrocie z dłuższej trasy czeka kilkanaście dni do obrobienia.

autoframe powstał żeby rozwiązać ten problem. Uruchamiasz skrypt w katalogu dnia, wracasz po kilku minutach — highlight gotowy. Model CLIP ocenia każdą scenę semantycznie (rozumie co jest w kadrze, nie tylko ruch czy jasność), wybiera najlepsze ujęcia, przeplata materiał z dwóch kamer i miesza z muzyką dobraną do charakteru nagrania. Bez ręcznego montażu, bez przeglądania godzin materiału.

Pipeline obsługuje kamerę kaskową (helmet cam) i kamerę 360° Insta360 X2 montowaną na lusterku — łącznie potrafi przetworzyć ponad 700 GB materiału z jednego dnia jazdy.

---

## Jak to działa

Pipeline zamienia surowy materiał z całego dnia w 3–5 minutowy highlight bez ręcznego montażu.

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

Model `ViT-L-14` OpenCLIP (wagi OpenAI) na GPU. Klatki przetwarzane w paczkach (domyślnie 64) — cały batch do GPU jednym `encode_image()`. Dla każdej klatki:

```
pos_score   = średnie podobieństwo cosinusowe do wszystkich promptów pozytywnych
neg_score   = średnie podobieństwo cosinusowe do wszystkich promptów negatywnych
final_score = pos_score - neg_score × neg_weight
```

`neg_weight` domyślnie 0.5. Wyniki trafiają do `_autoframe/scene_scores.csv` i są cache'owane.

**5. Selekcja scen** (`select_scenes.py`)

Sceny filtrowane i wybierane osobno dla każdego pliku źródłowego:

- Tylko sceny powyżej `threshold` (domyślnie 0.148).
- Każdy plik ma limit `max_per_file_sec` (domyślnie 45s).
- Słabo oceniane pliki dostają ostrzejszy limit przez system tierów (`tier1_cutoff`/`tier2_cutoff`).
- Każda scena przycinana do `max_scene_sec` (domyślnie 10s), wyśrodkowana na środku klipu.
- Klipy krótsze niż `min_take_sec` po przycięciu odrzucane.

W trybie dual-camera sceny z kamery A i B przeplatane chronologicznie. Kamera A jest źródłem audio; kamera B jest wyciszona. Wynik: `_autoframe/selected_scenes.txt`.

**5.5. Proxy reframe VID_ (opcjonalnie)** (`proxy_reframe.py`)

Jeśli `vid_input_format` jest ustawiony w sekcji `[reframe]` config.ini, pipeline zastępuje wybrane klipy LRV odpowiadającymi plikami `VID_` w wysokiej rozdzielczości (2880×2880 dual fisheye). Mapowanie: `LRV_TIMESTAMP_11_NNN` → `VID_TIMESTAMP_10_NNN.insv`. Czas wycięcia wyznaczany na podstawie CSV detekcji scen — ten sam środek co LRV, ale przetworzone przez `v360=dfisheye:rectilinear`. Wynik w `_autoframe/vid_trimmed/`.

Bez `vid_input_format` krok jest pomijany — w finalnym filmie zostają klipy LRV.

**6. Składanie highlightu** (`ffmpeg`)

Wybrane sceny łączone w `highlight.mp4`. Skalowanie do 4K (Lanczos), normalizacja do 60 fps (CFR), kodowanie NVENC jeśli dostępny, inaczej libx264. 4K jest celowe — YouTube przydziela znacznie więcej bitrate do uploadów 4K.

**7. Intro i outro** (`ffmpeg drawtext`)

Tło intro: klatka z najwyższym score CLIP. Nad nią dwie linie fontem Caveat Bold: rok + nazwa trasy (auto z nazwy katalogu lub `--title`). Outro: czarna plansza z konfigurowalnym tekstem. Fade in/out. Montaż przez stream copy do `highlight_final.mp4`.

**8. Dobór i miks muzyki** (`music_index.py` + `ffmpeg`)

Biblioteka muzyczna analizowana raz i cache'owana w `index.json`. Dla każdego utworu `librosa` wyciąga BPM i energię RMS (normalizowaną 0–1 względem całej biblioteki).

Dobór: średni score CLIP mapowany na docelową energię. Filtrowane są utwory co najmniej tak długie jak wideo, sortowane po bliskości długości. Finalny wybór losowany z top 5 kandydatów — różne utwory przy kolejnych uruchomieniach.

Miks: `amix` ffmpeg, oryginalne audio × `original_volume`, muzyka × `music_volume`. Muzyka przycinana do długości wideo i wygaszana. Wynik: `highlight_final_music.mp4`.

**Auto-generowanie config.ini** (`generate_config.py`)

Flaga `--about "opis dnia"` wywołuje Claude Haiku API przed uruchomieniem pipeline. Model generuje kompletny `config.ini` z `[clip_prompts]` i `[scene_selection]` dopasowanymi do opisanego materiału. Istniejący `config.ini` kopiowany do `config.ini.bak`. Wymaga zmiennej środowiskowej `ANTHROPIC_API_KEY`.

**Kaskada konfiguracji**

`<repo>/config.ini` przechowuje globalne domyślne. Jeśli w bieżącym katalogu (folder dnia) istnieje `config.ini`, jest wczytywany po globalnym i jego wartości mają pierwszeństwo.

---

## Wymagania systemowe

- Ubuntu 24.04 LTS
- GPU NVIDIA z CUDA (testowane: RTX 3070 Ti, driver 550)
- Python 3.12
- ~4 GB VRAM (model ViT-L-14)

---

## Instalacja

### 1. Pakiety systemowe

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip rename
```

Potrzebny ffmpeg z NVENC. Standardowy pakiet Ubuntu zazwyczaj nie ma NVENC — użyj jellyfin-ffmpeg:

```bash
# jellyfin-ffmpeg (działa z driverem 550+)
curl -fsSL https://repo.jellyfin.org/ubuntu/jellyfin_team.gpg.key \
    | sudo gpg --dearmor -o /usr/share/keyrings/jellyfin.gpg
echo "deb [signed-by=/usr/share/keyrings/jellyfin.gpg] https://repo.jellyfin.org/ubuntu jammy main" \
    | sudo tee /etc/apt/sources.list.d/jellyfin.list
sudo apt update && sudo apt install -y jellyfin-ffmpeg7
```

Po instalacji ustaw w `config.ini`:
```ini
[paths]
ffmpeg  = /usr/lib/jellyfin-ffmpeg/ffmpeg
ffprobe = /usr/lib/jellyfin-ffmpeg/ffprobe
```

Alternatywnie: zwykły ffmpeg z apt wystarczy jeśli NVENC nie jest potrzebny (fallback na libx264).

Weryfikacja:
```bash
/usr/lib/jellyfin-ffmpeg/ffmpeg -encoders 2>/dev/null | grep nvenc
nvidia-smi
```

### 2. Klonowanie repo

```bash
git clone https://github.com/OWNER/ai-autoframe ~/ai-autoframe
```

### 3. Python venv

```bash
python3 -m venv ~/highlight-env
source ~/highlight-env/bin/activate
```

### 4. PyTorch z CUDA

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
```

Weryfikacja:
```bash
python3 -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### 5. Pakiety Python

```bash
pip install \
    open-clip-torch==3.3.0 \
    scenedetect[opencv] \
    librosa==0.11.0 \
    pandas \
    Pillow \
    numpy \
    tqdm \
    soundfile \
    anthropic
```

### 5b. decord z obsługą CUDA (opcjonalnie, ~2× szybsza detekcja)

`decord` z PyPI dekoduje wideo na CPU. Żeby używać NVDEC (sprzętowy dekoder GPU), trzeba skompilować ze źródeł. Wymaga zainstalowanego CUDA Toolkit.

```bash
# Zależności do kompilacji
sudo apt install -y cmake build-essential git

# Klonowanie
git clone --recursive https://github.com/dmlc/decord ~/decord
cd ~/decord && mkdir build && cd build

# Patche dla FFmpeg 6+/7+ (niezgodności API)
# 1. Brakujący include bsf.h
sed -i '/#include <libavcodec\/avcodec.h>/a #include <libavcodec\/bsf.h>' \
    ~/decord/src/video/ffmpeg/ffmpeg_common.h

# 2. Wyłączenie audio (niezgodne API channel_layout w FFmpeg 7.x)
sed -i 's|src/\*.cc src/runtime/\*.cc src/video/\*.cc src/sampler/\*.cc src/audio/\*.cc src/av_wrapper/\*.cc|src/*.cc src/runtime/*.cc src/video/*.cc src/sampler/*.cc src/av_wrapper/*.cc|' \
    ~/decord/CMakeLists.txt

# Kompilacja (podaj właściwe compute capability swojego GPU: 86=RTX 30xx, 89=RTX 40xx)
CUDACXX=/usr/local/cuda-12.8/bin/nvcc cmake .. \
    -DUSE_CUDA=ON \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CUDA_ARCHITECTURES=86 \
    -DCMAKE_CXX_FLAGS="-fpermissive"
make -j$(nproc)

# Instalacja Python binding (w aktywnym venv)
source ~/highlight-env/bin/activate
pip install -e ~/decord/python/
```

Weryfikacja:
```bash
python3 -c "import decord; decord.gpu(0); print('decord CUDA OK')"
```

Bez CUDA build pipeline działa z CPU fallback — detekcja jest wolniejsza ale poprawna.

### 6. Font

```bash
mkdir -p ~/fonts
# Pobierz Caveat-Bold.ttf i umieść w ~/fonts/
# https://fonts.google.com/specimen/Caveat
```

---

## Struktura katalogów

```
~/
├── ai-autoframe/           # katalog repo
│   ├── autoframe.sh        # główny skrypt pipeline
│   ├── clip_score.py       # scoring CLIP
│   ├── select_scenes.py    # selekcja scen i przeplatanie
│   ├── proxy_reframe.py    # zastępowanie LRV klipy VID_ high-res
│   ├── music_index.py      # indeksowanie biblioteki muzycznej
│   ├── gpu_detect.py       # detekcja scen GPU (decord + PyTorch)
│   ├── generate_config.py  # generowanie config.ini przez Claude API
│   └── config.ini          # globalne domyślne
├── fonts/
│   └── Caveat-Bold.ttf
├── highlight-env/          # Python venv
└── moto/
    ├── music/              # biblioteka MP3/M4A
    │   └── index.json      # auto-generowany indeks BPM/energii
    └── 2025/
        └── 08-Rumunia/
            └── 12/         # katalog eventu (CWD przy uruchomieniu)
                ├── *.mp4
                ├── config.ini          # opcjonalnie: nadpisania per event
                ├── helmet/             # opcjonalnie: podfolder kamery A
                ├── 360/                # opcjonalnie: pliki .insv Insta360 (cam-b)
                │   ├── LRV_*.insv      # low-res proxy equirectangular (do detekcji)
                │   └── VID_*.insv      # high-res dual fisheye (do finalnego renderu)
                └── _autoframe/         # katalog roboczy (tworzony automatycznie)
                    ├── autocut/        # podzielone sceny
                    ├── frames/         # klatki kluczowe dla CLIP
                    ├── csv/            # pliki CSV detekcji scen
                    ├── trimmed/        # przycięte/wyciszone klipy
                    ├── reframed/       # klipy LRV po reprojekcji v360
                    ├── vid_trimmed/    # klipy VID_ high-res (proxy reframe)
                    └── scene_scores.csv
```

Wyniki pipeline:
```
highlight.mp4               # surowy highlight (bez intro/outro)
highlight_final.mp4         # z intro/outro
highlight_final_music.mp4   # finalny z muzyką
```

> **Uwaga:** Nazwy plików MP4 muszą być małymi literami. Jeśli kamera generuje nazwy wielkimi literami:
> ```bash
> rename 'y/A-Z/a-z/' *.MP4
> ```

---

## Kroki pipeline

| Krok | Opis |
|------|------|
| 0 | Reframe 360° — reprojekcja `LRV_*.insv` → flat MP4 przez `v360` (auto gdy cam-b zawiera .insv) |
| 1 | Znalezienie plików MP4 |
| 2 | Detekcja scen — `scenedetect` (CPU) lub `gpu_detect.py` (GPU, `--gpudetect`) |
| 3 | Podział — każda scena jako osobny plik w `autocut/` |
| 4 | Ekstrakcja klatek kluczowych — klatka ze środka każdego klipu (>5MB) |
| 5 | Scoring CLIP — `ViT-L-14` na GPU, prompty pozytywne vs. negatywne |
| 6 | Selekcja scen — filtr progu, limity tierów, cap per-plik, przycięcie do środka |
| 6.5 | Proxy reframe VID_ — zastąpienie klipy LRV wersjami high-res (opcjonalnie, gdy `vid_input_format` w config.ini) |
| 7 | Concat → `highlight.mp4` (NVENC jeśli dostępny, fallback libx264) |
| 8 | Intro (najlepsza klatka + tytuł) + outro → `highlight_final.mp4` |
| 9 | Miks muzyczny → `highlight_final_music.mp4` |

Wyniki kroków 0–5 są cache'owane — ponowne uruchomienie pomija już wykonaną pracę.

---

## Użycie

```bash
cd ~/moto/2025/08-Rumunia/12
source ~/highlight-env/bin/activate
~/ai-autoframe/autoframe.sh [opcje]
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
~/ai-autoframe/autoframe.sh --threshold 0.15

# Auto-generowanie config.ini opisem dnia (Claude Haiku API)
~/ai-autoframe/autoframe.sh --about "Transfăgărășan, górskie serpentyny, słoneczna pogoda, dramatyczne widoki"

# Dwie kamery: kamera przednia + kamera 360° Insta360 X2
~/ai-autoframe/autoframe.sh --cam-a helmet --cam-b 360

# Dwie kamery z opisem (reframe 360° auto-wykrywany)
~/ai-autoframe/autoframe.sh --cam-a helmet --cam-b 360 --about "Grecja, kręte drogi, widoki na morze"

# GPU detekcja scen
~/ai-autoframe/autoframe.sh --gpudetect

# Konkretna muzyka
~/ai-autoframe/autoframe.sh --music-genre "progressive house" --music-artist "elektronomia"
~/ai-autoframe/autoframe.sh --music-artist "tobu,janji,alan"

# Bez intro, bez muzyki
~/ai-autoframe/autoframe.sh --no-intro --no-music

# Własny tytuł
~/ai-autoframe/autoframe.sh --title "Transalpina\nSierpień 2025"
```

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

---

## Per-event config.ini

Żeby nadpisać ustawienia dla konkretnego eventu bez dotykania globalnego configa, umieść `config.ini` w katalogu eventu. Wystarczy podać tylko sekcje i klucze które chcesz zmienić — reszta jest dziedziczona z `<repo>/config.ini`.

Przykład — ciemny wąwóz, lokalne prompty:

```ini
[clip_prompts]
positive =
    motorcycle riding through narrow gorge canyon limestone rock walls towering
    road at bottom of deep canyon vertical cliffs both sides
    tight canyon road rock face inches from motorcycle

negative =
    boring flat highway with no scenery
    parking lot or gas station
    blurry or out of focus image

[clip_scoring]
neg_weight = 0.3

[scene_selection]
threshold = 0.138
max_per_file_sec = 75
max_scene_sec = 12
min_take_sec = 3
```

> **Prompty powinny opisywać to co faktycznie jest w materiale.** Nie dodawaj promptów dla lokalizacji których nie ma w nagraniach — CLIP uśrednia po wszystkich promptach pozytywnych, więc nieistotne rozmywają score.

---

## Biblioteka muzyczna

### Dodawanie muzyki

```bash
# NCS — pobierz z playlisty YouTube
yt-dlp -x --audio-format mp3 \
    "PLAYLIST_URL" \
    -o "$HOME/moto/music/%(title)s.%(ext)s"
```

Gatunek wykrywany automatycznie z formatu NCS: `Artist - Title ｜ Genre ｜ NCS - Copyright Free Music`.

### Indeks muzyczny

```bash
~/ai-autoframe/autoframe.sh --music-rebuild
# albo bezpośrednio:
python3 ~/ai-autoframe/music_index.py ~/moto/music
```

`index.json` przechowuje: BPM, energię 0–1, czas trwania, gatunek. Usunięte pliki są usuwane z indeksu automatycznie.

---

## Wykluczanie scen

Żeby trwale wykluczyć scenę z highlightu, ustaw jej score na 0 w `_autoframe/scene_scores.csv`:

```bash
# Wyklucz konkretną scenę
awk -F',' -v OFS=',' '$1=="PLIK-scene-014"{$2=0; $3=0; $4=0} 1' \
    _autoframe/scene_scores.csv > /tmp/tmp.csv && mv /tmp/tmp.csv _autoframe/scene_scores.csv

# Wyklucz wszystkie sceny z danego pliku
awk -F',' -v OFS=',' '/^PLIK/{$2=0; $3=0; $4=0} 1' \
    _autoframe/scene_scores.csv > /tmp/tmp.csv && mv /tmp/tmp.csv _autoframe/scene_scores.csv
```

Po zmianie usuń `_autoframe/selected_scenes.txt` i `highlight*.mp4` żeby wymusić re-render.

---

## Dokumentacja config.ini

Wszystkie domyślne wartości pipeline są w `<repo>/config.ini`. Flagi CLI mają pierwszeństwo nad wartościami z configa. Per-event `config.ini` w katalogu dnia ma pierwszeństwo nad globalnym.

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

Każdy prompt w osobnej linii (wcięcie = kontynuacja wartości INI). Im bardziej specyficzny prompt (konkretna góra, kraj, typ drogi), tym lepiej model rozróżnia dobry materiał.

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

Sekcja aktywna gdy cam-b zawiera pliki `LRV_*.insv`. Kąty wymagają jednorazowej kalibracji.

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `yaw` | `0` | Obrót poziomy. `0`=przód, `90`=bok, `180`=tył. |
| `pitch` | `0` | Pochylenie. `0`=poziom, ujemne=w dół. Zakres: `-180` do `180`. |
| `roll` | `0` | Rotacja obrazu. `90` lub `-90` do korekcji przekrzywionej kamery. |
| `h_fov` | `100` | Poziome pole widzenia w stopniach. |
| `v_fov` | `75` | Pionowe pole widzenia w stopniach. |
| `vid_input_format` | *(brak)* | Format wejściowy plików VID_ dla proxy reframe. Ustaw `dfisheye` dla Insta360 X2. Bez tego klucza krok 6.5 jest pomijany. |
| `vid_ih_fov` | `190` | Pole widzenia wejściowe dla `v360` przy przetwarzaniu VID_ (dual fisheye). |

**Kalibracja kąta** (jednorazowo dla nowego montażu):
```bash
for yaw in 0 90 180 270; do
  for pitch in -90 -45 0 45 90; do
    ffmpeg -i LRV_*.insv \
      -vf "v360=equirect:rectilinear:yaw=${yaw}:pitch=${pitch}:h_fov=100:v_fov=75" \
      -frames:v 1 "test_y${yaw}_p${pitch}.jpg" -y -loglevel quiet 2>/dev/null
  done
done
```
Znajdź klatkę z właściwym widokiem, dodaj korekcję `roll` jeśli obraz jest przekrzywiony.

**Przykładowe wartości dla kamery na lusterku, stick 1m w górę, widok do tyłu:**
```ini
[reframe]
yaw   = 90
pitch = 160
roll  = 90
h_fov = 100
v_fov = 70
vid_input_format = dfisheye
vid_ih_fov       = 190
```

### `[paths]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `venv` | `~/highlight-env` | Wirtualne środowisko Python. |
| `work_subdir` | `_autoframe` | Podkatalog roboczy w katalogu każdego dnia. |
| `ffmpeg` | `ffmpeg` | Ścieżka do ffmpeg. Ustaw gdy używasz jellyfin-ffmpeg lub własnego builda. |
| `ffprobe` | `ffprobe` | Ścieżka do ffprobe. |

---

---

# autoframe — AI Highlight Reel Pipeline

Every day on a motorcycle produces hundreds of gigabytes of raw footage from one or two cameras. Manually editing that volume — reviewing, cutting, assembling — takes many times longer than the ride itself. After a longer trip there are weeks of footage waiting to be processed.

autoframe was built to solve this. Run the script in the day's directory, come back in a few minutes — highlight done. A CLIP model scores each scene semantically (it understands what is in the frame, not just motion or brightness), picks the best shots, interleaves footage from two cameras, and mixes in music matched to the character of the ride. No manual editing, no scrubbing through hours of footage.

The pipeline handles a helmet camera and an Insta360 X2 360° camera mounted on the mirror — capable of processing over 700 GB of footage from a single day of riding.

---

## How it works

The pipeline turns a day's worth of raw footage into a 3–5 minute highlight reel without manual editing.

**0. 360° reframe — LRV proxy** (`ffmpeg v360`)

If `--cam-b` points to a directory containing `LRV_*.insv` files (Insta360 X2), the pipeline automatically reprojects them to a rectilinear view using ffmpeg's `v360` filter. LRV files are low-res proxies (~736×368) already stitched to equirectangular by the camera — a single reprojection pass is all that is needed. Output MP4s land in `_autoframe/reframed/` and are treated as regular camera B files.

**1. Scene detection** (`scenedetect` or `gpu_detect.py`)

Default: each source MP4 passes through PySceneDetect's `detect-content` algorithm — frame-to-frame colour histogram differences. With `--gpudetect`: frames decoded by `decord` in chunks of 128, downscaled to 64×64, compared with MAD (mean absolute difference). Output is a scenedetect-compatible CSV — caching works identically. If decord does not have CUDA support, it automatically falls back to CPU decoding.

Detection output is cached — re-running skips already processed files.

**2. Scene splitting** (`scenedetect split-video`)

Each detected scene extracted as a separate MP4 via stream copy (no re-encoding). Files land in `_autoframe/autocut/`.

**3. Key frame extraction** (`ffmpeg`)

For each clip larger than 5 MB, a single JPEG is extracted at the midpoint. Clips under 5 MB are skipped. Frames cached in `_autoframe/frames/`.

**4. CLIP scoring** (`clip_score.py`, ViT-L-14 on GPU)

OpenCLIP's `ViT-L-14` (OpenAI weights) runs on the GPU. Frames processed in batches (default 64). For each frame:

```
pos_score   = mean cosine similarity to all positive prompts
neg_score   = mean cosine similarity to all negative prompts
final_score = pos_score - neg_score × neg_weight
```

Scores land in `_autoframe/scene_scores.csv` and are cached.

**5. Scene selection** (`select_scenes.py`)

Scenes filtered and selected per source file:
- Only scenes above `threshold` (default 0.148).
- Each source file capped at `max_per_file_sec` (default 45s).
- Low-scoring files get a tighter cap via the tier system.
- Each scene trimmed to `max_scene_sec` (default 10s), centred on midpoint.
- Clips shorter than `min_take_sec` after trimming are discarded.

In dual-camera mode, scenes from camera A and B are interleaved chronologically. Camera A is the audio source; camera B is muted. Output: `_autoframe/selected_scenes.txt`.

**5.5. VID_ proxy reframe (optional)** (`proxy_reframe.py`)

If `vid_input_format` is set in the `[reframe]` section of config.ini, the pipeline replaces selected LRV clips with corresponding high-resolution `VID_` files (2880×2880 dual fisheye). Mapping: `LRV_TIMESTAMP_11_NNN` → `VID_TIMESTAMP_10_NNN.insv`. Timing is derived from the scene detection CSV — same midpoint as the LRV clip, but processed through `v360=dfisheye:rectilinear`. Output in `_autoframe/vid_trimmed/`.

Without `vid_input_format`, the step is skipped — the final video uses LRV-quality clips from cam-b.

**6. Highlight assembly** (`ffmpeg`)

Selected scenes concatenated into `highlight.mp4`. Upscaled to 4K (Lanczos), normalised to 60 fps (CFR), encoded with NVENC if available, otherwise libx264. 4K output is intentional — YouTube allocates significantly more bitrate to 4K uploads.

**7. Intro and outro** (`ffmpeg drawtext`)

Best-scoring frame as intro background. Two lines of Caveat Bold text: year + trip name (auto from directory path, or via `--title`). Outro: plain black card with configurable text. Both cards fade in/out. Final sequence assembled with stream copy into `highlight_final.mp4`.

**8. Music selection and mix** (`music_index.py` + `ffmpeg`)

Music library analysed once and cached in `index.json`. `librosa` extracts BPM and RMS energy (normalised 0–1 across the library). Average CLIP score mapped to energy target. Final pick chosen randomly from top 5 candidates — ensures variety across runs. Output: `highlight_final_music.mp4`.

**Auto-generating config.ini** (`generate_config.py`)

The `--about "description"` flag calls the Claude Haiku API before the pipeline starts. The model generates a complete `config.ini` with `[clip_prompts]` and `[scene_selection]` tailored to the described footage. Existing `config.ini` backed up to `config.ini.bak`. Requires `ANTHROPIC_API_KEY` environment variable.

**Config cascade**

`<repo>/config.ini` holds global defaults. A `config.ini` in the current working directory (day folder) takes precedence.

---

## System Requirements

- Ubuntu 24.04 LTS
- NVIDIA GPU with CUDA (tested: RTX 3070 Ti, driver 550)
- Python 3.12
- ~4 GB VRAM (ViT-L-14 model)

---

## Installation

### 1. System packages

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip rename
```

ffmpeg with NVENC is required. The standard Ubuntu package typically lacks NVENC — use jellyfin-ffmpeg:

```bash
curl -fsSL https://repo.jellyfin.org/ubuntu/jellyfin_team.gpg.key \
    | sudo gpg --dearmor -o /usr/share/keyrings/jellyfin.gpg
echo "deb [signed-by=/usr/share/keyrings/jellyfin.gpg] https://repo.jellyfin.org/ubuntu jammy main" \
    | sudo tee /etc/apt/sources.list.d/jellyfin.list
sudo apt update && sudo apt install -y jellyfin-ffmpeg7
```

Then set in `config.ini`:
```ini
[paths]
ffmpeg  = /usr/lib/jellyfin-ffmpeg/ffmpeg
ffprobe = /usr/lib/jellyfin-ffmpeg/ffprobe
```

Alternatively, the standard `apt` ffmpeg works if NVENC is not needed (fallback to libx264).

### 2. Clone repo

```bash
git clone https://github.com/OWNER/ai-autoframe ~/ai-autoframe
```

### 3. Python venv

```bash
python3 -m venv ~/highlight-env
source ~/highlight-env/bin/activate
```

### 4. PyTorch with CUDA

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
```

Verify:
```bash
python3 -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### 5. Python packages

```bash
pip install \
    open-clip-torch==3.3.0 \
    scenedetect[opencv] \
    librosa==0.11.0 \
    pandas \
    Pillow \
    numpy \
    tqdm \
    soundfile \
    anthropic
```

### 5b. decord with CUDA support (optional, ~2× faster detection)

`decord` from PyPI decodes video on CPU. To use NVDEC (GPU hardware decoder), you need to build from source. Requires CUDA Toolkit installed.

```bash
# Build dependencies
sudo apt install -y cmake build-essential git

# Clone
git clone --recursive https://github.com/dmlc/decord ~/decord
cd ~/decord && mkdir build && cd build

# Patches for FFmpeg 6+/7+ API incompatibilities
# 1. Missing bsf.h include
sed -i '/#include <libavcodec\/avcodec.h>/a #include <libavcodec\/bsf.h>' \
    ~/decord/src/video/ffmpeg/ffmpeg_common.h

# 2. Disable audio (channel_layout API changed in FFmpeg 7.x)
sed -i 's|src/\*.cc src/runtime/\*.cc src/video/\*.cc src/sampler/\*.cc src/audio/\*.cc src/av_wrapper/\*.cc|src/*.cc src/runtime/*.cc src/video/*.cc src/sampler/*.cc src/av_wrapper/*.cc|' \
    ~/decord/CMakeLists.txt

# Build (set compute capability for your GPU: 86=RTX 30xx, 89=RTX 40xx)
CUDACXX=/usr/local/cuda-12.8/bin/nvcc cmake .. \
    -DUSE_CUDA=ON \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CUDA_ARCHITECTURES=86 \
    -DCMAKE_CXX_FLAGS="-fpermissive"
make -j$(nproc)

# Install Python binding (activate venv first)
source ~/highlight-env/bin/activate
pip install -e ~/decord/python/
```

Verify:
```bash
python3 -c "import decord; decord.gpu(0); print('decord CUDA OK')"
```

Without the CUDA build the pipeline falls back to CPU decoding — detection is slower but correct.

### 6. Font

```bash
mkdir -p ~/fonts
# Download Caveat-Bold.ttf and place it in ~/fonts/
# https://fonts.google.com/specimen/Caveat
```

---

## Directory Structure

```
~/
├── ai-autoframe/           # repo directory
│   ├── autoframe.sh        # main pipeline script
│   ├── clip_score.py       # CLIP scoring
│   ├── select_scenes.py    # scene selection and interleaving
│   ├── proxy_reframe.py    # replace LRV clips with VID_ high-res
│   ├── music_index.py      # music library indexing
│   ├── gpu_detect.py       # GPU scene detection (decord + PyTorch)
│   ├── generate_config.py  # config.ini generation via Claude API
│   └── config.ini          # global defaults
├── fonts/
│   └── Caveat-Bold.ttf
├── highlight-env/          # Python venv
└── moto/
    ├── music/              # MP3/M4A library
    │   └── index.json      # auto-generated BPM/energy index
    └── 2025/
        └── 08-Romania/
            └── 12/         # event directory (CWD when running)
                ├── *.mp4
                ├── config.ini          # optional: per-event overrides
                ├── helmet/             # optional: camera A subfolder
                ├── 360/                # optional: Insta360 .insv files (cam-b)
                │   ├── LRV_*.insv      # low-res proxy for detection/scoring
                │   └── VID_*.insv      # high-res dual fisheye for final render
                └── _autoframe/         # working directory (auto-created)
                    ├── autocut/        # split scenes
                    ├── frames/         # key frames for CLIP
                    ├── csv/            # scene detection CSV files
                    ├── trimmed/        # trimmed/muted scene clips
                    ├── reframed/       # reprojected LRV clips (v360)
                    ├── vid_trimmed/    # VID_ high-res clips (proxy reframe)
                    └── scene_scores.csv
```

Output files:
```
highlight.mp4               # raw highlight (no intro/outro)
highlight_final.mp4         # with intro/outro
highlight_final_music.mp4   # final with music
```

> **Note:** MP4 filenames must be lowercase. If your camera generates uppercase names:
> ```bash
> rename 'y/A-Z/a-z/' *.MP4
> ```

---

## Pipeline Steps

| Step | Description |
|------|-------------|
| 0 | 360° reframe — reproject `LRV_*.insv` → flat MP4 via `v360` (auto when cam-b contains .insv) |
| 1 | Find MP4 files |
| 2 | Scene detection — `scenedetect` (CPU) or `gpu_detect.py` (GPU, `--gpudetect`) |
| 3 | Split — each scene as a separate file in `autocut/` |
| 4 | Key frame extraction — midpoint frame of each clip (>5MB) |
| 5 | CLIP scoring — `ViT-L-14` on GPU, positive vs. negative prompts |
| 6 | Scene selection — threshold filter, tier limits, per-file cap, trim to centre |
| 6.5 | VID_ proxy reframe — replace LRV clips with high-res versions (optional, requires `vid_input_format` in config.ini) |
| 7 | Concat → `highlight.mp4` (NVENC if available, fallback to libx264) |
| 8 | Intro (best frame + title) + outro → `highlight_final.mp4` |
| 9 | Music mix → `highlight_final_music.mp4` |

Steps 0–5 results are cached — re-running skips already completed work.

---

## Usage

```bash
cd ~/moto/2025/08-Romania/12
source ~/highlight-env/bin/activate
~/ai-autoframe/autoframe.sh [options]
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
--about "TEXT"        Describe the day's ride — auto-generates config.ini via Claude API (Haiku)
--reframe             Force 360° reframe even without auto-detected .insv files
--help                Show help
```

### Examples

```bash
# Basic run
~/ai-autoframe/autoframe.sh --threshold 0.15

# Auto-generate config.ini from day description (Claude Haiku API)
~/ai-autoframe/autoframe.sh --about "Transfăgărășan, mountain hairpins, sunny weather, dramatic views"

# Two cameras: front camera + Insta360 X2 360°
~/ai-autoframe/autoframe.sh --cam-a helmet --cam-b 360

# Two cameras with description (360° reframe auto-detected)
~/ai-autoframe/autoframe.sh --cam-a helmet --cam-b 360 --about "Greece, winding coastal roads, sea views"

# GPU scene detection
~/ai-autoframe/autoframe.sh --gpudetect

# Specific music
~/ai-autoframe/autoframe.sh --music-genre "progressive house" --music-artist "elektronomia"
~/ai-autoframe/autoframe.sh --music-artist "tobu,janji,alan"

# Skip intro, no music
~/ai-autoframe/autoframe.sh --no-intro --no-music

# Custom title
~/ai-autoframe/autoframe.sh --title "Transalpina\nAugust 2025"
```

### Partial re-render (using cache)

```bash
# Re-mix music only
rm highlight_final_music.mp4

# Re-render intro/outro
rm _autoframe/intro.mp4 _autoframe/outro.mp4 _autoframe/highlight_faded.mp4 highlight_final.mp4

# Re-run from scene selection onwards
rm _autoframe/selected_scenes.txt highlight.mp4 highlight_final.mp4 highlight_final_music.mp4

# Force full re-score (change of prompts or neg_weight)
rm _autoframe/scene_scores.csv

# Re-reframe LRV 360° (changed angle in config.ini)
rm -rf _autoframe/reframed/

# Re-render VID_ proxy clips (changed angle or vid_input_format)
rm -rf _autoframe/vid_trimmed/
```

---

## Per-event config.ini

To override settings for a specific event without touching the global config, place a `config.ini` in the event directory. Only include the sections and keys you want to change — the rest is inherited from `<repo>/config.ini`.

Example — dark canyon footage with location-specific CLIP prompts:

```ini
[clip_prompts]
positive =
    motorcycle riding through narrow gorge canyon limestone rock walls towering
    road at bottom of deep canyon vertical cliffs both sides
    tight canyon road rock face inches from motorcycle

negative =
    boring flat highway with no scenery
    parking lot or gas station
    blurry or out of focus image

[clip_scoring]
neg_weight = 0.3

[scene_selection]
threshold = 0.138
max_per_file_sec = 75
max_scene_sec = 12
min_take_sec = 3
```

> **Prompts should describe what is actually in the footage.** Do not add prompts for locations or conditions not present in the material — CLIP averages over all positive prompts, so irrelevant ones dilute the score.

---

## Music Library

### Adding music

```bash
# NCS — download from YouTube playlist
yt-dlp -x --audio-format mp3 \
    "PLAYLIST_URL" \
    -o "$HOME/moto/music/%(title)s.%(ext)s"
```

Genre is detected automatically from the NCS filename format:
`Artist - Title ｜ Genre ｜ NCS - Copyright Free Music`.

### Music index

```bash
~/ai-autoframe/autoframe.sh --music-rebuild
# or directly:
python3 ~/ai-autoframe/music_index.py ~/moto/music
```

`index.json` stores: BPM, energy 0–1, duration, genre. Deleted files are automatically removed from the index.

---

## Excluding scenes

To permanently exclude a scene from the highlight, set its score to 0 in `_autoframe/scene_scores.csv`:

```bash
# Exclude a specific scene
awk -F',' -v OFS=',' '$1=="FILE-scene-014"{$2=0; $3=0; $4=0} 1' \
    _autoframe/scene_scores.csv > /tmp/tmp.csv && mv /tmp/tmp.csv _autoframe/scene_scores.csv

# Exclude all scenes from a source file
awk -F',' -v OFS=',' '/^FILE/{$2=0; $3=0; $4=0} 1' \
    _autoframe/scene_scores.csv > /tmp/tmp.csv && mv /tmp/tmp.csv _autoframe/scene_scores.csv
```

Then delete `_autoframe/selected_scenes.txt` and `highlight*.mp4` to force a re-render.

---

## config.ini Reference

All pipeline defaults live in `<repo>/config.ini`. CLI flags take precedence over config values. A per-event `config.ini` in the day folder takes precedence over the global one.

### `[scene_detection]`

| Key | Default | Description |
|-----|---------|-------------|
| `threshold` | `20` | CPU scene cut detector sensitivity. Lower = more cuts. Range typically 10–40. |
| `min_scene_len` | `8s` | Minimum detected scene duration. Use `s` suffix (e.g. `5s`, `15s`). |
| `gpu_threshold` | `30` | MAD threshold for `--gpudetect`. Different scale to `threshold` — calibrate separately. |

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

Each prompt on its own line (indentation = INI value continuation). The more specific the prompt, the better the model distinguishes good footage.

### `[clip_scoring]`

| Key | Default | Description |
|-----|---------|-------------|
| `top_percent` | `25` | Percentage of top scenes printed in the summary log. Display only. |
| `neg_weight` | `0.5` | Weight of negative prompts: `final = pos - neg * neg_weight`. Lower to `0.3` for dark or shadowy footage. |
| `batch_size` | `64` | Frames processed by GPU at once. Lower to `32`/`16` if you get OOM errors. |

### `[scene_selection]`

| Key | Default | Description |
|-----|---------|-------------|
| `threshold` | `0.148` | Minimum CLIP score. Typical range: 0.13–0.16. |
| `max_scene_sec` | `10` | Max seconds from a single scene clip. Trimmed to midpoint. |
| `max_per_file_sec` | `45` | Max total seconds from a single source file. |
| `tier1_cutoff` | `0.145` | Files with best scene below this threshold → capped at `tier1_limit`. |
| `tier1_limit` | `10` | Time cap (seconds) for tier-1 files. |
| `tier2_cutoff` | `0.150` | Files between `tier1_cutoff` and this threshold → capped at `tier2_limit`. |
| `tier2_limit` | `20` | Time cap (seconds) for tier-2 files. |
| `min_take_sec` | `0.5` | Clips shorter than this after trimming are discarded. |

### `[video]`

| Key | Default | Description |
|-----|---------|-------------|
| `resolution` | `3840:2160` | Output resolution. 4K = more bitrate on YouTube. |
| `framerate` | `60` | Output framerate. |
| `audio_bitrate` | `192k` | Audio bitrate. |
| `nvenc_cq` | `18` | NVENC constant quality (lower = better). |
| `nvenc_preset` | `p5` | NVENC preset. `p1`=fastest, `p7`=best quality. |
| `x264_crf` | `15` | libx264 CRF quality (fallback). |
| `x264_preset` | `fast` | libx264 speed preset. |

### `[intro_outro]`

| Key | Default | Description |
|-----|---------|-------------|
| `duration` | `3` | Duration of intro and outro cards (seconds). |
| `fade_duration` | `1` | Fade in/out duration. |
| `outro_text` | `Editing powered by AI` | Outro card text. |
| `font` | `~/fonts/Caveat-Bold.ttf` | TTF/OTF font file for drawtext. |
| `font_size_title` | `120` | Font size — intro first line. |
| `font_size_subtitle` | `96` | Font size — intro second line. |
| `font_size_outro` | `60` | Font size — outro card. |

### `[music]`

| Key | Default | Description |
|-----|---------|-------------|
| `dir` | `~/moto/music` | Directory with MP3/M4A files. |
| `music_volume` | `0.7` | Music volume in final mix (0–1). |
| `original_volume` | `0.3` | Original audio volume. Set to `0` to silence completely. |
| `fade_out_duration` | `3` | Music fade-out duration at end of video (seconds). |

### `[reframe]`

Active when cam-b contains `LRV_*.insv` files. Angles require one-time calibration per camera mount.

| Key | Default | Description |
|-----|---------|-------------|
| `yaw` | `0` | Horizontal rotation. `0`=front, `90`=side, `180`=rear. |
| `pitch` | `0` | Vertical tilt. `0`=level, negative=down. Range: `-180` to `180`. |
| `roll` | `0` | Image rotation. Use `90`/`-90` to correct a tilted camera. |
| `h_fov` | `100` | Horizontal field of view in degrees. |
| `v_fov` | `75` | Vertical field of view in degrees. |
| `vid_input_format` | *(unset)* | Input format for VID_ proxy reframe. Set to `dfisheye` for Insta360 X2. Without this key, step 6.5 is skipped. |
| `vid_ih_fov` | `190` | Input field of view for `v360` when processing VID_ dual fisheye files. |

**Angle calibration** (one-time, per camera mount):
```bash
for yaw in 0 90 180 270; do
  for pitch in -90 -45 0 45 90; do
    ffmpeg -i LRV_*.insv \
      -vf "v360=equirect:rectilinear:yaw=${yaw}:pitch=${pitch}:h_fov=100:v_fov=75" \
      -frames:v 1 "test_y${yaw}_p${pitch}.jpg" -y -loglevel quiet 2>/dev/null
  done
done
```
Find the frame with the correct view, add `roll` correction if the image is tilted.

**Example values for mirror mount, 1m vertical stick, rear-facing view:**
```ini
[reframe]
yaw   = 90
pitch = 160
roll  = 90
h_fov = 100
v_fov = 70
vid_input_format = dfisheye
vid_ih_fov       = 190
```

### `[paths]`

| Key | Default | Description |
|-----|---------|-------------|
| `venv` | `~/highlight-env` | Python virtual environment path. |
| `work_subdir` | `_autoframe` | Working subdirectory created inside each day folder. |
| `ffmpeg` | `ffmpeg` | Path to ffmpeg binary. Set when using jellyfin-ffmpeg or a custom build. |
| `ffprobe` | `ffprobe` | Path to ffprobe binary. |
