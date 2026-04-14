# Biblioteka muzyczna / Music library

## PL

### Dodawanie muzyki

```bash
# NCS — pobierz z playlisty YouTube
yt-dlp -x --audio-format mp3 \
    "PLAYLIST_URL" \
    -o "$HOME/moto/music/NCS/%(title)s.%(ext)s"
```

Pliki mogą mieć dowolną nazwę. Gatunek wykrywany automatycznie z formatu NCS:
`Artist - Title ｜ Genre ｜ NCS - Copyright Free Music`

### Budowanie indeksu

Indeks budowany przez Web UI: zakładka **Music** → przycisk **↺ Update index**.

- **re-analyze** — wymusza ponowne liczenie BPM i energii dla wszystkich plików (`--force`)
- **re-genres** — odświeża tylko gatunki przez Last.fm bez ponownej analizy audio (`--force-genres`)

Paski postępu pokazują rzeczywisty postęp analizy pliku po pliku. Usunięte pliki są usuwane z indeksu automatycznie przy kolejnym uruchomieniu. Już zindeksowane pliki są pomijane.

Indeks przechowywany w `index.json` w katalogu muzycznym. Pola: BPM, energia (0–1), czas trwania, gatunek, wykonawca, tytuł.

### Gatunek — źródła w kolejności priorytetów

1. Tagi osadzone w pliku (ID3/iTunes) — odczytywane przez `ffprobe`
2. Wzorzec nazwy pliku `｜ Genre ｜` (konwencja NCS)
3. Last.fm API — tylko jeśli ustawiony `LAST_FM_API_KEY` w `.env`

### Wybór muzyki przez Web UI

Zakładka **Music**:
- Filtruj po tytule/wykonawcy wpisując tekst w pole Filter
- Filtruj po gatunku przez dropdown
- Kliknij ▶ żeby odsłuchać utwór — seek bar pojawi się pod tytułem
- Zaznacz checkboxy przy wybranych ścieżkach
- Zaznaczone ścieżki trafiają do pipeline przy kolejnym renderze

Filtr czasu trwania: pokazywane są tylko ścieżki o czasie trwania zbliżonym do szacowanego czasu highlight (±5s). Jeśli żadna ścieżka nie pasuje — rozszerz bibliotekę lub zmień threshold w Select scenes.

### Weryfikacja Content ID (ACRCloud)

Przed użyciem muzyki na YouTube warto sprawdzić czy utwór nie jest zarejestrowany w Content ID. ACRCloud to serwis audio-fingerprint używany przez wiele platform.

**Ręcznie:** przycisk **⚙** przy ścieżce w zakładce Music → wynik pojawi się jako plakietka `✓ Free` lub `⚠ Claimed`.

**Automatycznie:** gdy ustawione są zmienne `ACRCLOUD_*` w `.env`, pipeline przed każdym renderem (bez ręcznie wybranego utworu) sprawdza kandydatów po kolei i pomija zgłoszone — w logu zobaczysz `ACR check: <nazwa> … ✓ Free → using <nazwa>`.

```
# .env
ACRCLOUD_HOST=identify-eu-west-1.acrcloud.com
ACRCLOUD_ACCESS_KEY=your_key
ACRCLOUD_ACCESS_SECRET=your_secret
```

Darmowy plan: 100 rozpoznań/dzień. Rejestracja: [console.acrcloud.com](https://console.acrcloud.com) → projekt **Audio & Video Recognition**.

Before using music on YouTube it's worth checking whether the track is registered in Content ID. ACRCloud is an audio-fingerprint service used by many platforms.

**Manually:** the **⚙** button next to a track in the Music tab → result shown as `✓ Free` or `⚠ Claimed` badge.

**Automatically:** when `ACRCLOUD_*` vars are set in `.env`, the pipeline checks candidates before every render (when no track is manually pinned) and skips claimed ones — the log shows `ACR check: <name> … ✓ Free → using <name>`.

Free plan: 100 recognitions/day. Register at [console.acrcloud.com](https://console.acrcloud.com) → project type **Audio & Video Recognition**.

---

### Logika doboru utworu

Pipeline mapuje średni score CLIP wszystkich wybranych scen na docelową energię muzyki:

```
energy_target = (avg_score - 0.14) × 10   (obcięte do 0.2–0.9)
```

Materiał wysoko oceniany → energetyczna muzyka. Materiał słabo oceniany → spokojna. Finalny wybór losowany z top 5 kandydatów — różne utwory przy kolejnych renderach tego samego materiału.

---

## EN

### Adding music

```bash
# NCS — download from YouTube playlist
yt-dlp -x --audio-format mp3 \
    "PLAYLIST_URL" \
    -o "$HOME/moto/music/NCS/%(title)s.%(ext)s"
```

Files can have any name. Genre detected automatically from NCS filename format:
`Artist - Title ｜ Genre ｜ NCS - Copyright Free Music`

### Building the index

Built via Web UI: **Music** tab → **↺ Update index** button.

- **re-analyze** — forces BPM and energy re-analysis for all files
- **re-genres** — refreshes genres via Last.fm only, no audio re-analysis

A real per-file progress bar tracks the analysis. Deleted files are removed from the index automatically on the next run. Already indexed files are skipped.

Index stored in `index.json` in the music directory. Fields: BPM, energy (0–1), duration, genre, artist, title.

### Genre — source priority

1. Embedded file tags (ID3/iTunes) — read via `ffprobe`
2. Filename pattern `｜ Genre ｜` (NCS convention)
3. Last.fm API — only if `LAST_FM_API_KEY` is set in `.env`

### Music selection via Web UI

**Music** tab:
- Filter by title/artist using the text filter
- Filter by genre using the dropdown
- Click ▶ to preview a track — seek bar appears inline below the title
- Check boxes next to selected tracks
- Checked tracks are used in the next render

Duration filter: only tracks within ±5s of the estimated highlight duration are shown. If no tracks match — expand the library or adjust the Select scenes threshold.

### Track selection logic

Average CLIP score of selected scenes mapped to music energy target:

```
energy_target = (avg_score - 0.14) × 10   (clamped 0.2–0.9)
```

High-scoring footage → energetic music. Final pick chosen randomly from top 5 candidates — ensures variety across renders of the same footage.

---

### Pobieranie z YouTube i plakietka licencji / YouTube download and license badge

Pobieranie przez zakładkę Music → pole **YouTube** → **↓ Download** automatycznie zapisuje metadane źródłowego wideo (licencja, URL, kanał) w pliku `.yt.json` obok MP3. Przy kolejnym przebudowaniu indeksu dane trafiają do `index.json` i widoczne są jako plakietka:

- Zielone **CC** — Creative Commons (film źródłowy miał licencję CC na YouTube)
- Czerwone **©** — standardowy copyright — wysokie ryzyko Content ID

Plakietka to wskazówka, nie gwarancja: właściciel CC może równolegle zarejestrować utwór w Content ID. Dla pewności użyj weryfikacji ACRCloud.

Downloading via the Music tab → **YouTube** field → **↓ Download** automatically saves source video metadata (license, URL, channel) in a `.yt.json` sidecar alongside the MP3. On the next index rebuild the data is included in `index.json` and shown as a badge:

- Green **CC** — Creative Commons (the source video had a CC license on YouTube)
- Red **©** — standard copyright — high Content ID risk

The badge is a hint, not a guarantee: a CC owner can simultaneously register the track in Content ID. For certainty use the ACRCloud check.
