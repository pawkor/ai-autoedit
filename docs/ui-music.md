# Zakładka Music / Music tab

Zakładka **Music** służy do wyboru ścieżki muzycznej przed renderem music-driven. Wyświetla bibliotekę z katalogu ustawionego w Settings → Music directory.

The **Music** tab is used to select a music track before a music-driven render. It shows the library from the directory set in Settings → Music directory.

---

## Lista utworów / Track list

Każdy wiersz przedstawia jeden plik audio. Kolumny:

Each row represents one audio file. Columns:

| Kolumna | Opis / Description |
|---------|-------------------|
| ☐ | Checkbox — pin track (zastępuje auto-select). / Checkbox — pins the track (overrides auto-select). |
| ▶ | Odtwórz/zatrzymaj podgląd. / Play/stop preview. |
| ✕ | Usuń plik z dysku (widoczny po najechaniu). / Delete file from disk (visible on hover). |
| ⚙ | ACRCloud Content ID check (widoczny po najechaniu). / ACRCloud Content ID check (visible on hover). |
| Title / Artist | Tytuł i artysta. Czerwona kropka **●** = utwór użyty wcześniej w innym projekcie (tooltip: projekt, render, data, link YT). / Title and artist. Red dot **●** = track used before in another project (tooltip: project, render, date, YT link). |
| Genre | Gatunek muzyczny (z Last.fm lub tagu ID3). |
| Dur | Czas trwania. |
| BPM | Tempo w uderzeniach na minutę. |
| Energy | Energia audio (0–1). |
| Copyright | Wynik ACRCloud: **CC** = Creative Commons, **©** = zastrzeżone, **YT** = licencja YouTube. Ukryta gdy brak wyniku. / ACRCloud result: **CC** = Creative Commons, **©** = restricted, **YT** = YouTube license. Hidden when no result. |

### Wskaźnik użytego utworu / Used track indicator

Czerwona kropka **●** przy tytule oznacza że ten utwór był już użyty w jakimś renderze (globalnie, niezależnie od projektu). Najechanie na kropkę pokazuje tooltip z listą projektów:

The red **●** dot next to a title means this track has already been used in a render (globally, across all projects). Hovering shows a tooltip with entries like:

```
2026-04 Grecja  2025-04-Grecja-04.24_v4  2026-04-09  YT: https://...
```

Dane przechowywane w `webapp/jobs/used_tracks.json`. Utwór pozostaje na liście — możesz świadomie wybrać go ponownie.

Data stored in `webapp/jobs/used_tracks.json`. The track stays on the list — you can consciously reuse it.

---

## Nagłówek listy / List header

Nagłówek zawiera trzy sekcje:

The header contains three sections:

- **Lewa strona (cols 1–4):** przycisk `↺ est.` (przelicz szacowany czas), pole **offset (s)** i pole **± tolerance (s)**. Offset jest automatycznie uzupełniany wartością `intro_outro.duration` z config po wczytaniu projektu.
- **Title / Artist** — klikalny nagłówek sortowania (kol. 5).
- Pozostałe nagłówki kolumn — klikalne do sortowania.

- **Left side (cols 1–4):** `↺ est.` button (recalculate estimated duration), **offset (s)** field, and **± tolerance (s)** field. The offset is auto-populated from `intro_outro.duration` in config when the project loads.
- **Title / Artist** — clickable sort header (col 5).
- Other column headers — clickable for sorting.

**↺ est.** przelicza sortowanie listy według dopasowania do szacowanego czasu highlight. Sorter preferuje utwór którego długość najbliżej pasuje do celu.

**↺ est.** recalculates the list order by fit to the estimated highlight duration. The sorter prefers the track whose length is the closest match to the target.

**Offset (s)** — czas muzyki do przeskoczenia na początku (np. gdy utwór ma 4s intro bez beatów). Auto-wypełniany z `intro_outro.duration`.

**Offset (s)** — seconds to skip at the start of the music track (e.g. if the track has a 4s beatless intro). Auto-filled from `intro_outro.duration`.

**± tolerance (s)** — tolerancja przy filtrze długości. Np. cel = 240s, tolerance = 20s → akceptuje 220–260s.

**± tolerance (s)** — tolerance for duration matching. E.g. target = 240s, tolerance = 20s → accepts 220–260s.

---

## Stopka / Footer

Stopka wyświetla aktualnie wybrany utwór (auto lub pinowany) oraz przyciski akcji.

The footer shows the currently selected track (auto or pinned) and action buttons.

| Element | Opis / Description |
|---------|-------------------|
| 🔀 Auto-select | Automatycznie dobiera najlepszy utwór (bpm, energia, czas). Ignoruje utwór jeśli ACR oznaczył jako zablokowany. / Auto-selects the best-matching track (bpm, energy, duration). Ignores ACR-blocked tracks. |
| Wiersz utworu | Nazwa i BPM wybranego pliku. / Name and BPM of the selected file. |
| **⚠** | Czerwony trójkąt — widoczny gdy wybrany utwór był już użyty w innym projekcie. Pozwala świadomie zdecydować przed renderem. / Red triangle — visible when the selected track was already used in another project. Lets you decide consciously before rendering. |

---

## Filtr i sortowanie / Filter and sort

Pole **filter** przeszukuje po tytule, artyście i czasie trwania (prefix, np. `4:3` → 4:30–4:39).

The **filter** field searches by title, artist, and duration (prefix, e.g. `4:3` → 4:30–4:39).

Kliknięcie nagłówka kolumny sortuje listę. Ponowne kliknięcie odwraca kolejność. Sortowanie po **Dur** używa szacowanego czasu highlight jako celu — utwór najbardziej dopasowany pojawia się na górze.

Clicking a column header sorts the list. Clicking again reverses. Sorting by **Dur** uses the estimated highlight duration as target — best-matching track appears at top.

---

## ACRCloud Content ID

Przycisk ⚙ przy każdym utworze wysyła 10-sekundowy fragment do ACRCloud i sprawdza czy utwór jest chroniony prawem autorskim.

The ⚙ button next to each track sends a 10-second fingerprint to ACRCloud to check copyright status.

| Wynik / Result | Znaczenie |
|---------------|-----------|
| **CC** | Creative Commons — bezpieczny do użycia |
| **©** | Zastrzeżone — ryzyko Content ID na YT |
| **YT** | Licencja YouTube — tylko na YouTube |

Wyniki zapisywane są do `index.json` i przeżywają reload strony.

Results are saved to `index.json` and persist across page reloads.

Auto-check (`_acr_preselect`) uruchamiany automatycznie przed renderem gdy brak pinowanego utworu.

Auto-check (`_acr_preselect`) runs automatically before render when no track is pinned.

---

## Dodawanie muzyki / Adding music

W górnym pasku Music tab: pole URL YouTube + przycisk **↓ yt-dlp**. Po pobraniu plik trafia do katalogu muzyki, sidecar `.yt.json` zapisuje licencję, link i kanał. Odznaka **CC** lub **YT** pojawia się w kolumnie Copyright.

In the Music tab top bar: YouTube URL field + **↓ yt-dlp** button. After download the file lands in the music directory; a `.yt.json` sidecar stores the license, URL, and channel. A **CC** or **YT** badge appears in the Copyright column.
