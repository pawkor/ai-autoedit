# Zakładka Gallery / Gallery tab

![Galeria scen](img/AI-autoedit-gallery.png)

Gallery pokazuje klatkę środkową każdej wykrytej sceny z jej wynikiem CLIP, posortowane chronologicznie.

Gallery shows the midpoint frame of each detected scene with its CLIP score, sorted chronologically.

---

## Threshold

Suwak **Threshold** (z przyciskami ▼/▲ po 0.001) filtruje które sceny wejdą do highlight. Sceny powyżej progu mają pomarańczową ramkę (included), poniżej — szarą (excluded). Licznik nad galerią pokazuje ile scen przeszło próg i szacowany czas.

The **Threshold** slider (with ▼/▲ buttons stepping 0.001) filters which scenes go into the highlight. Scenes above the threshold have an orange border (included), below — grey (excluded). The counter above the gallery shows how many scenes passed and the estimated duration.

### Szacowany czas / Duration estimate

Licznik pokazuje różne wartości w zależności od stanu:

- **Po renderze** (threshold bez zmian, brak nowych overrides) — dokładny wynik ostatniego rendera.
- **Po zmianie threshold lub overrides** — estymacja z dry-runu (dokładny Python, nie aproksymacja JS), aktualizowana po ok. 1s od zatrzymania suwaka.
- **Dual-camera** — wynik uwzględnia sparowane sceny z drugiej kamery (`cam_ratio`).

The counter reflects different states:
- **After render** (same threshold, no new overrides) — exact result of the last render.
- **After threshold/override change** — dry-run estimate (exact Python, not JS approximation), updated ~1s after the slider stops.
- **Dual-camera** — result accounts for paired back-cam scenes (`cam_ratio`).

## Limit per file

Sceny które przeszły threshold, ale zostały odcięte przez `max_per_file_sec`, oznaczone są bursztynową ramką z oznaczeniem **limit**. Kliknięcie takiej sceny force-include'uje ją (z pominięciem limitu).

Scenes that passed the threshold but were cut by `max_per_file_sec` are shown with an amber border and **limit** badge. Clicking such a scene force-includes it (bypassing the cap).

## Manualne overrides / Manual overrides

Kliknięcie klatki przełącza jej status:
- **Included → force-exclude** (ciemna ramka, ikona ×)
- **Excluded → force-include** (zielona ramka, ikona ✓)
- **Manual → reset** (powrót do decyzji threshold)

Overrides zapisywane są po stronie serwera w `_autoframe/manual_overrides.json` i stosowane przy każdym kolejnym renderze.

Clicking a frame toggles its status:
- **Included → force-exclude** (dark border, × icon)
- **Excluded → force-include** (green border, ✓ icon)
- **Manual → reset** (back to threshold decision)

Overrides are saved server-side in `_autoframe/manual_overrides.json` and applied on every subsequent render.

## Filter

Dwa pola tekstowe obok przycisku Reset filtrują widoczne sceny:

| Pole | Placeholder | Działanie |
|------|-------------|-----------|
| Score | `score` | Prefiks score — np. `0.8` pokazuje sceny 0.800–0.899 |
| Time | `HH:MM` | Prefiks czasu — np. `09` pokazuje sceny z godziny 09:xx |

Wciśnięcie Enter lub opuszczenie pola stosuje filtr. Oba filtry można łączyć.

Two text inputs next to the Reset button filter the visible scenes:

| Field | Placeholder | Behaviour |
|-------|-------------|-----------|
| Score | `score` | Score prefix — e.g. `0.8` shows scenes 0.800–0.899 |
| Time | `HH:MM` | Timestamp prefix — e.g. `09` shows scenes with 09:xx timecodes |

Press Enter or blur to apply. Both filters can be combined.

## ↺ Reset

Przywraca threshold do wartości wyznaczonej automatycznie podczas analizy (top-10 scen) i usuwa wszystkie manualne overrides.

Restores the threshold to the value auto-computed during analysis (top-10 scenes) and clears all manual overrides.

## → Music

Przycisk w prawym górnym rogu galerii przenosi bezpośrednio na zakładkę Music.

Button in the top-right of the gallery switches directly to the Music tab.
