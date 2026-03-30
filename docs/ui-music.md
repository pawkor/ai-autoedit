# Zakładka Music / Music tab

![Muzyka](img/AI-autoedit-music.png)

Zakładka **Music** pokazuje zaindeksowane ścieżki z katalogu muzycznego (MP3/M4A) z wykonawcą, tytułem, BPM, energią i gatunkiem. Indeks ładuje się automatycznie przy otwieraniu projektu.

The **Music** tab shows indexed tracks from the music directory (MP3/M4A) with artist, title, BPM, energy, and genre. The index loads automatically when a project is opened.

---

## Filtrowanie / Filtering

- Pole tekstowe **Filter** — filtruje po tytule lub wykonawcy
- Dropdown **genre** — filtruje po gatunku

Pokazywane są tylko ścieżki o czasie trwania zbliżonym do szacowanego czasu highlight (±5s). Jeśli lista jest pusta — rozszerz bibliotekę lub zmień threshold w Gallery.

Only tracks within ±5s of the estimated highlight duration are shown. If the list is empty — expand the library or adjust the Gallery threshold.

## Podgląd / Preview

Kliknięcie ▶ przy ścieżce uruchamia odtwarzanie. Pod tytułem pojawia się suwak seek do przewijania utworu. Kliknięcie ▶ ponownie lub przy innej ścieżce zatrzymuje poprzedni utwór.

Clicking ▶ next to a track starts playback. A seek bar appears below the title for scrubbing. Clicking ▶ again or on another track stops the previous one.

## Zaznaczanie / Selection

Checkboxy przy ścieżkach zaznaczają je do użycia w pipeline. Zaznaczenie przenosi się na kolejne rendery. Przyciski **All** / **None** zaznaczają lub odznaczają wszystkie widoczne (po filtrach) ścieżki.

Checkboxes next to tracks mark them for use in the pipeline. Selection persists across renders. **All** / **None** buttons select or deselect all currently visible (filtered) tracks.

## ↺ Update index

Przebudowuje indeks BPM/energii/gatunków. Rzeczywisty pasek postępu pokazuje analizę pliku po pliku.

Rebuilds the BPM/energy/genre index. A real per-file progress bar tracks the analysis.

| Checkbox | Działanie |
|----------|-----------|
| **re-analyze** | Wymusza ponowne liczenie BPM i energii dla wszystkich plików |
| **re-genres** | Odświeża tylko gatunki przez Last.fm, bez ponownej analizy audio |

Szczegóły biblioteki muzycznej, budowania indeksu i logiki doboru: [Biblioteka muzyczna](music.md).

Details on the music library, index building, and selection logic: [Music library](music.md).
