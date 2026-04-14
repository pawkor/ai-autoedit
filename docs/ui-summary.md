# Zakładka Render / Render tab


Zakładka **Render** zbiera wyniki analizy i daje dostęp do renderowania.

The **Render** tab aggregates analysis results and provides access to rendering.

---

## Analysis results

| Pole | Opis |
|------|------|
| Scenes detected | Łączna liczba wykrytych scen |
| Scenes selected | Liczba scen wybranych do highlight (po threshold, overrides i balansowaniu kamer) |
| Scoring | Aktualny próg CLIP (zsynchronizowany z Select scenes) |
| Est. duration | Szacowany czas highlight na podstawie wybranych scen |

Wartości aktualizują się na żywo po każdej zmianie threshold lub kliknięciu klatki w Select scenes.

Values update live after every threshold change or Select scenes frame click.

| Field | Description |
|-------|-------------|
| Scenes detected | Total number of detected scenes |
| Scenes selected | Scenes selected for the highlight (after threshold, overrides, and camera balancing) |
| Scoring | Current CLIP threshold (synced with Select scenes) |
| Est. duration | Estimated highlight duration based on selected scenes |

---

## Music selection

Pokazuje wybraną ścieżkę muzyczną lub informację o braku zaznaczonej muzyki. Link **→ Change in Music tab** przenosi bezpośrednio do zakładki Music.

Shows the selected music track or a notice if no music is selected. **→ Change in Music tab** link navigates directly to the Music tab.

---

## ♪ Music-driven (domyślny tryb / default mode)

Generuje highlight zsynchronizowany ze strukturą muzyczną — uderzenia, segmenty, energia. Nie wymaga ustawiania threshold ani Max scene / Per file.

Generates a highlight synchronised to the music structure — beats, segments, energy. No threshold or Max scene / Per file settings needed.

- Wybierz utwór w zakładce Music (lub zostaw auto-select) → **♪ Music-driven**
- Klipy dobierane z pełnej puli scen (`scene_scores_allcam.csv` gdy istnieje — wszystkie kamery)
- Każda scena użyta maksymalnie raz (`used` set); source diversity przez rolling window
- Różnorodność kamer: naprzemienne cięcia back/helmet przy każdym ujęciu
- Łuk chronologiczny: klipy z rana na początku, wieczorne (zachód słońca) przy fade-out — z metadanych `creation_time`
- Wynik: `*-md_v1.mp4`, kolejne rundy `md_v2`, `md_v3…` — poprzednie nie są nadpisywane
- Guzik wraca po potwierdzeniu startu — można kolejkować kolejne rendery

Select a track in the Music tab (or leave auto-select) → **♪ Music-driven**.
- Clips drawn from the full scene pool (`scene_scores_allcam.csv` when present — all cameras)
- Each scene used at most once; source diversity enforced via rolling window
- Camera diversity: back/helmet alternate on every cut
- Chronological arc: morning clips open the edit, evening clips (sunsets) close it — derived from source file `creation_time` metadata
- Output: `*-md_v1.mp4`, subsequent runs `md_v2`, `md_v3…` — previous versions not overwritten
- Button re-enables after confirmed start — multiple renders can be queued

---

## ▶ Render Highlight *(Traditional mode)*

Dostępny po włączeniu **Traditional mode** w Advanced modal (⚙ Advanced → Traditional mode).

Available after enabling **Traditional mode** in the Advanced modal (⚙ Advanced → Traditional mode).

Uruchamia finalne enkodowanie z bieżącym threshold i overrides. Pod przyciskiem pojawia się pasek postępu z ETA aktualizowany w czasie rzeczywistym.

Starts final encoding with the current threshold and overrides. A real-time progress bar with ETA appears below the button.

Pipeline renderuje: selekcja scen → przycinanie → concat → intro/outro → miks muzyczny → plik wynikowy (np. `2025-04-Grecja-04.21.mp4`).

The pipeline renders: scene selection → trimming → concat → intro/outro → music mix → output file (e.g. `2025-04-Grecja-04.21.mp4`).

Kolejne rendery z nową muzyką lub innym threshold tworzą nowy plik (v2, v3…) — poprzednie wersje nie są nadpisywane.

Subsequent renders with new music or a different threshold create a new file (v2, v3…) — previous versions are not overwritten.

---

## ▶ Render Short

Generuje klip pionowy 9:16 (YouTube Shorts) z `make_shorts.py`.

Generates a vertical 9:16 clip (YouTube Shorts) using `make_shorts.py`.

- Top-scored scenes (per-camera normalization w multicam), 1.5s shots, center crop do 9:16 (1080×1920)
- Losowy offset w scenie: pomija pierwsze 20% (stabilizacja kamery po cięciu) i ostatnie 10%
- Xfade transitions między ujęciami (zoomin, radial, fadewhite…)
- Muzyka z `/data/music/shorts/` — onset density, rotacja przez `shorts_used.json`
- Pasek postępu procentowy (`[X/Y]` z loga) + log w Log tab
- Wynik: `2025-04-Grecja-04.21-short_v01.mp4` w work_dir, auto-increment `vNN`

Output: `2025-04-Grecja-04.21-short_v01.mp4` in work_dir. Each run auto-increments `vNN`. Re-run with a different `--seed` (CLI) to get different frame offsets from the same scenes.

> Text overlays (`--text`) dostępne z CLI ale domyślnie wyłączone — hashtagi w video nie mają wartości algorytmicznej na YouTube.
