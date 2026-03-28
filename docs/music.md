# Biblioteka muzyczna / Music library

## PL

### Dodawanie muzyki

```bash
# NCS — pobierz z playlisty YouTube
yt-dlp -x --audio-format mp3 \
    "PLAYLIST_URL" \
    -o "$HOME/moto/music/%(title)s.%(ext)s"
```

Pliki mogą mieć dowolną nazwę. Gatunek wykrywany automatycznie z formatu NCS:
`Artist - Title ｜ Genre ｜ NCS - Copyright Free Music`

### Budowanie indeksu

```bash
# Pierwsze indeksowanie lub po dodaniu nowych plików
~/ai-autoedit/autoframe.sh --music-rebuild

# Albo bezpośrednio
source ~/highlight-env/bin/activate
python3 ~/ai-autoedit/music_index.py ~/moto/music
```

`index.json` przechowuje: BPM, energię 0–1, czas trwania, gatunek. Usunięte pliki są usuwane z indeksu automatycznie. Już zindeksowane pliki są pomijane.

### Logika doboru utworu

Pipeline mapuje średni score CLIP wszystkich scen na docelową energię:

```
energy_target = (avg_score - 0.14) × 10   (obcięte do 0.2–0.9)
```

Materiał wysoko oceniany → energetyczna muzyka. Materiał słabo oceniany → spokojna.

Filtrowane są utwory co najmniej tak długie jak wideo, sortowane po bliskości długości. Finalny wybór losowany z top 5 kandydatów — różne utwory przy kolejnych uruchomieniach na tym samym materiale.

### Filtrowanie muzyki

```bash
# Tylko konkretny gatunek
~/ai-autoedit/autoframe.sh --music-genre "progressive house"

# Konkretny artysta lub lista artystów
~/ai-autoedit/autoframe.sh --music-artist "elektronomia"
~/ai-autoedit/autoframe.sh --music-artist "tobu,janji,alan"

# Kombinacja
~/ai-autoedit/autoframe.sh --music-genre "dnb" --music-artist "high maintenance"
```

---

## EN

### Adding music

```bash
# NCS — download from YouTube playlist
yt-dlp -x --audio-format mp3 \
    "PLAYLIST_URL" \
    -o "$HOME/moto/music/%(title)s.%(ext)s"
```

Files can have any name. Genre detected automatically from NCS filename format:
`Artist - Title ｜ Genre ｜ NCS - Copyright Free Music`

### Building the index

```bash
~/ai-autoedit/autoframe.sh --music-rebuild
# or directly:
python3 ~/ai-autoedit/music_index.py ~/moto/music
```

`index.json` stores: BPM, energy 0–1, duration, genre. Deleted files are removed automatically. Already indexed files are skipped.

### Track selection logic

Average CLIP score of all scenes mapped to energy target:

```
energy_target = (avg_score - 0.14) × 10   (clamped 0.2–0.9)
```

High-scoring footage → energetic music. Low-scoring footage → calm music.

Tracks filtered to those at least as long as the video, sorted by duration proximity. Final pick chosen randomly from top 5 — ensures variety across runs.

### Filtering music

```bash
~/ai-autoedit/autoframe.sh --music-genre "progressive house"
~/ai-autoedit/autoframe.sh --music-artist "elektronomia"
~/ai-autoedit/autoframe.sh --music-artist "tobu,janji,alan"
```
