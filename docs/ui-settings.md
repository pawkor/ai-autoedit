# Zakładka Settings / Settings tab

Zakładka **Settings** pozwala zmieniać wszystkie parametry pipeline bez edytowania plików. Każde pole zapisuje się do `config.ini` projektu automatycznie po opuszczeniu pola (Enter lub kliknięcie gdzie indziej).

The **Settings** tab lets you change all pipeline parameters without editing files. Every field saves to the project's `config.ini` automatically on blur or Enter.

---

## Sekcje / Sections

### Source / Proxy media

Katalog roboczy z plikami MP4 (tylko do odczytu — ustawiony przy tworzeniu projektu). Przycisk 📁 otwiera przeglądarkę plików dla tego katalogu (patrz niżej).

Working directory with MP4 files (read-only — set when the project is created). The 📁 button opens the file browser for that directory (see below).

**Konfiguracja kamer / Camera configuration**

![Ustawienia](img/AI-autoedit-settings.png)

Lista podkatalogów kamer. Cam A = źródło audio; pozostałe kamery są wyciszane w mikście. Przycisk **+ Add camera** dodaje kolejny wiersz. Przycisk 📁 przy każdej kamerze otwiera przeglądarkę plików dla tego podkatalogu.

List of camera subdirectories. Cam A = audio source; other cameras are muted in the mix. **+ Add camera** adds a row. The 📁 button next to each camera opens the file browser for that subfolder.

**Przesunięcia zegarowe kamer / Camera clock offsets**

Pole **Clock offset (s)** przy każdej kamerze pozwala skompensować stały dryft zegara — np. kask nagrany 2h za wcześnie (Helmet Ace Pro 2) → wpisz `7200`. Wartości zapisywane są w sekcji `[cam_offsets]` w `config.ini` projektu i stosowane przy synchronizacji multicam.

The **Clock offset (s)** field per camera compensates for a fixed clock drift — e.g. a helmet cam recorded 2h early → enter `7200`. Values are saved in `[cam_offsets]` in the project `config.ini` and applied during multicam sync.

**Proxy media**

Sekcja **Proxy media** (po prawej stronie Source) tworzy zmniejszone kopie plików źródłowych (480p, 20 fps CFR) używane jako wejście do wykrywania scen. Proxy drastycznie skraca czas detekcji przy zachowaniu identycznych cięć.

The **Proxy media** section (to the right of Source) creates downscaled copies of source files (480p, 20 fps CFR) used as input for scene detection. Proxies dramatically reduce detection time while producing identical cut points.

| Element | Opis / Description |
|---------|-------------------|
| Status | Liczba gotowych proxy / całkowita liczba plików źródłowych (np. `12/33`) |
| Pasek postępu | Widoczny podczas tworzenia; znika po zakończeniu |
| **▶ Create proxies** | Uruchamia tworzenie proxy sekwencyjnie dla brakujących plików. Proxy tworzone są atomowo (`.mp4.tmp` → `.mp4`), więc przerwanie jest bezpieczne — wznowienie pomija gotowe pliki |

Proxy są przechowywane w `_autoframe/proxy/` i nie wliczają się do plików wynikowych. Otworzenie projektu automatycznie uruchamia tworzenie proxy jeśli brakuje jakichkolwiek plików.

Proxies are stored in `_autoframe/proxy/` and are not included in output files. Opening a project automatically starts proxy creation if any files are missing.

---

### Scene detection *(requires Re-analyze)*

Przycisk **⚙ Advanced** otwiera modal z parametrami detekcji.

The **⚙ Advanced** button opens a modal with detection parameters.

| Parametr | Domyślnie | Opis / Description |
|----------|-----------|---------------------|
| Detect threshold | `20` (auto) | Czułość detektora. Auto-kalibrowany przed każdym runs. / Detector sensitivity. Auto-calibrated before each run. |
| Min scene len | `8` | Minimalna długość sceny w sekundach. / Minimum scene duration in seconds. |

**CLIP-first mode** *(domyślny)* — skanuje klatki co N sekund i ekstraktuje klipy wokół peaków CLIP score. Nie szuka cięć montażowych — szuka dobrego materiału. Wymagany re-analyze po zmianie. PySceneDetect dostępny jako fallback po odznaczeniu.

**CLIP-first mode** *(default)* — scans frames every N seconds and extracts clips around CLIP score peaks. Finds good content rather than edit cuts. Re-analyze required after change. PySceneDetect available as fallback when unchecked.

| Parametr CLIP-first | Domyślnie | Opis |
|--------------------|-----------|------|
| Interval (s) | `3` | Sekundy między próbkowanymi klatkami |
| Clip dur (s) | `8` | Długość wycinanego klipu |
| Min gap (s) | `30` | Min. odstęp między klipami |
| Score all cameras | `on` (auto) | Scoruje klatki ze wszystkich kamer → `scene_scores_allcam.csv`. Wymagane dla music-driven multicam. Automatycznie włączane przy zaznaczeniu CLIP-first. |

**⚠ Re-analyze badge** — obok przycisku Analyze pojawia się ostrzeżenie gdy ustawienia wykrywania nie zgadzają się z istniejącymi scenami (np. CLIP-first zaznaczony ale sceny są `-scene-NNN`).

The **⚠ Re-analyze badge** appears next to the Analyze button when detection settings don't match existing scenes (e.g. CLIP-first checked but scenes are `-scene-NNN`).

**Traditional mode** — checkbox w Advanced modal odkrywa: `▶ Render Highlight`, suwak Target dur., pola Max scene / Per file. Domyślnie ukryte — nie potrzebne w trybie music-driven.

**Traditional mode** — checkbox in the Advanced modal reveals: `▶ Render Highlight` button, Target dur. slider, Max scene / Per file fields. Hidden by default — not needed in music-driven mode.

---

### Scene selection *(Traditional mode only)*

| Parametr | Opis / Description |
|----------|--------------------|
| Max scene sec | Maks. czas wycinany z jednej sceny (środek klipu). / Max seconds taken per scene (centred). |
| Max per file sec | Maks. łączny czas z jednego pliku. / Max total seconds from one source file. |
| Target min | Docelowy czas highlight — używany przez auto threshold search i ⟳ Fill. / Target highlight duration — used by auto threshold search and ⟳ Fill. |

Przycisk **⟳ Fill** oblicza Max scene sec i Max per file sec automatycznie.

The **⟳ Fill** button auto-calculates Max scene sec and Max per file sec.

Threshold CLIP ustawiany jest automatycznie przez wyszukiwanie binarne (Target dur.) w zakładce Select scenes — obliczenia po stronie klienta, bez opóźnienia sieciowego.

The CLIP threshold is set automatically by the binary search (Target dur.) in the Select scenes tab — computed client-side, instant.

---

### GPS scoring

Pole **GPS weight** (suwak 0–1, domyślnie 0) aktywuje wzmocnienie score scen przez dane GPS z kamer Insta360. Gdy plik MP4 zawiera ścieżkę GPS (1 Hz), pipeline extraktuje prędkość (km/h) i kąt obrotu (°/s) dla każdej sceny. Score CLIP modyfikowany przed filtrem threshold:

```
score *= (1 + gps_weight × (speed_norm×0.7 + turn_norm×0.3))
```

Sceny z szybką jazdą na zakrętach zyskują wyższy priorytet. Wymaga narzędzia `exiftool` w kontenerze i plików z zapisanym GPS (Insta360 X3/X4 i podobne).

The **GPS weight** slider (0–1, default 0) enables GPS-based score boosting for Insta360 cameras. When the MP4 contains a GPS track (1 Hz), the pipeline extracts per-scene speed (km/h) and turn rate (°/s). CLIP score modified before threshold filter:

```
score *= (1 + gps_weight × (speed_norm×0.7 + turn_norm×0.3))
```

Scenes with fast cornering get higher priority. Requires `exiftool` in the container and GPS-enabled cameras (Insta360 X3/X4 or similar).

---

### Camera cut pattern *(Music-driven)*

Pole **Camera pattern** (np. `ab`, `aabaab`, `aabb`) steruje kolejnością kamer w music-driven render. Litery `a` i `b` odpowiadają Cam A i Cam B z sekcji Camera configuration powyżej. Puste = automatyczne przeplatanie na podstawie score (a/b wybierane per slot najlepszym dostępnym klipem).

Przykłady:
- `ab` — naprzemiennie 1:1
- `aabaab` — dwa ujęcia Cam A, jedno Cam B, powtarzane
- `aabb` — pary po dwa

The **Camera pattern** field (e.g. `ab`, `aabaab`, `aabb`) controls camera order in music-driven render. Letters `a` and `b` correspond to Cam A and Cam B from the Camera configuration section above. Empty = automatic score-driven alternation (best available clip per slot, no forced camera order).

Examples:
- `ab` — strict 1:1 alternation
- `aabaab` — two Cam A shots then one Cam B, repeating
- `aabb` — pairs of two

---

### Intro / music

| Parametr | Opis / Description |
|----------|--------------------|
| Title | Tytuł na kartce intro. Każda linia = jeden wiersz tekstu. Pozostaw puste dla auto (rok + folder). / Intro card title. Each line = one text row. Leave empty for auto (year + folder). |
| Music directory | Katalog z biblioteką MP3 do miksowania. / Directory with the MP3 library for mixing. |
| No intro/outro | Pomija generowanie karty intro i czarnego outro. / Skips intro card and black outro generation. |
| No music | Pomija miks muzyczny. / Skips the music mix. |

---

### CLIP prompts

Przycisk **AI / CLIP ↗** w prawym panelu (inspector) otwiera modal edytora promptów (80% szerokości × 80% wysokości okna).

The **AI / CLIP ↗** button in the right inspector panel opens the prompts editor modal (80% width × 80% height).

Modal zawiera:
- Pole **About this ride** (opis wyjazdu, 15 linii)
- Przycisk **✦ Generate** — wywołuje Claude API i generuje prompty na podstawie opisu
- Przycisk **Save** — zapisuje prompty do `config.ini` projektu
- Dwie kolumny: **POSITIVE** i **NEGATIVE** — po jednym prompcie na linię, edytowalne bezpośrednio

The modal contains:
- **About this ride** field (ride description, 15 rows)
- **✦ Generate** button — calls Claude API and generates prompts from the description
- **Save** button — saves prompts to the project's `config.ini`
- Two columns: **POSITIVE** and **NEGATIVE** — one prompt per line, directly editable

---

### CLIP scoring

| Parametr | Domyślnie | Opis / Description |
|----------|-----------|--------------------|
| Batch size | `64` | Klatki przez GPU jednocześnie. Obniż do 32/16 przy błędach OOM. / Frames per GPU batch. Lower to 32/16 if OOM errors occur. |
| Workers | `4` | Wątki ładowania klatek. / Frame-loading worker threads. |

---

### Shorts

Parametry generowania krótkich filmów (YouTube Shorts / Instagram Reels).

Parameters for short-form video generation (YouTube Shorts / Instagram Reels).

| Parametr | Opis / Description |
|----------|--------------------|
| **Text overlays** | Dodaje animowane hashtagi do shorta (flaga `--text` w `make_shorts.py`). / Adds animated hashtags to the short (`--text` flag in `make_shorts.py`). |
| **Crop X offsets** | Przesunięcie poziome kadru 9:16 per kamera (piksele). Format: `kamera=wartość`, jedna para na linię. Np. `back=-250` przesuwa kadr tylnej kamery o 250 px w lewo. Używane gdy obiekt jest poza środkiem kadru po automatycznym przycinaniu do 9:16. / Horizontal crop offset for 9:16 per camera (pixels). Format: `camera=value`, one pair per line. E.g. `back=-250` shifts the back camera crop 250 px left. Used when the subject is off-center after the automatic 9:16 crop. |

---

## Re-analyze with these settings

Uruchamia pipeline od nowa. Pomija detekcję scen jeśli pliki źródłowe i parametry `[scene_detection]` nie zmieniły się.

Reruns the pipeline. Skips scene detection if source files and `[scene_detection]` parameters haven't changed.

---

## Przeglądarka plików / File browser


Otwierana przyciskiem 📁 przy katalogu roboczym lub podkatalogu kamery. Wyświetla pliki wideo (MP4, MOV, MTS, M2TS i inne) w danym katalogu.

Opened via the 📁 button next to the working directory or a camera subfolder. Shows video files (MP4, MOV, MTS, M2TS, etc.) in that directory.

| Akcja | Opis |
|-------|------|
| Najechanie kursorem na plik | Podgląd wideo (miniaturka 240×135) |
| **×** przy pliku | Usuwa plik z dysku po potwierdzeniu |
| Checkbox przy pliku | Zaznacza do grupowego usunięcia |
| **Delete checked** | Usuwa zaznaczone pliki po potwierdzeniu |
| **+ Folder** | Tworzy nowy podkatalog |
| **↑ Upload** | Przesyła pliki z przeglądarki do tego katalogu |

---

## Experimental / Untested

### S3 source

Sekcja **S3 source** pojawia się automatycznie gdy w `.env` skonfigurowane są zmienne `S3_BUCKET`, `S3_ACCESS_KEY_ID` i `S3_SECRET_ACCESS_KEY`. Umożliwia pobieranie plików źródłowych wideo bezpośrednio z bucketa S3 przed uruchomieniem pipeline.

The **S3 source** section appears automatically when `S3_BUCKET`, `S3_ACCESS_KEY_ID`, and `S3_SECRET_ACCESS_KEY` are set in `.env`. It allows fetching source video files from an S3 bucket before running the pipeline.

Lista plików: ikona ☁ = tylko na S3, ✓ = już pobrane lokalnie. Checkbox przy pliku zaznacza go do pobrania.

File list: ☁ = S3 only, ✓ = already downloaded locally. Checkbox marks a file for download.

| Przycisk | Opis |
|----------|------|
| **↺** (nagłówek) | Odświeża listę plików z S3 |
| All missing | Zaznacza wszystkie pliki których brakuje lokalnie |
| **↓ Fetch selected** | Pobiera zaznaczone pliki z S3 do katalogu roboczego, pasek postępu per-plik |
| **✕ Purge local** | Usuwa lokalne pliki źródłowe i przetworzone klipy (`_autoframe/autocut/`) żeby zwolnić miejsce |

Konfiguracja S3 w `.env` / S3 configuration in `.env`:

```
S3_BUCKET=my-bucket
S3_ACCESS_KEY_ID=your-key-id
S3_SECRET_ACCESS_KEY=your-secret
S3_REGION=us-east-1
S3_ENDPOINT_URL=https://...   # opcjonalne: Backblaze B2, Cloudflare R2, MinIO / optional
```
