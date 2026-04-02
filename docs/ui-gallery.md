# Zakładka Gallery / Gallery tab

![Galeria scen](img/AI-autoedit-gallery.png)

Gallery pokazuje klatkę środkową każdej wykrytej sceny z jej wynikiem CLIP, posortowane chronologicznie.

Gallery shows the midpoint frame of each detected scene with its CLIP score, sorted chronologically.

---

## Target dur.

Pole **Target dur.** (format `m:ss`, np. `6:45`) ustawia docelowy czas highlight. Po wpisaniu i naciśnięciu Enter (lub zmianie wartości) uruchamia się automatyczne szukanie progu CLIP binarnym wyszukiwaniem (12 iteracji na backendzie z DRY_RUN), tak żeby uzyskać film jak najbliższy zadanemu czasowi. Wynik — liczba scen i szacowany czas — pojawia się w liczniku nad galerią.

The **Target dur.** field (format `m:ss`, e.g. `6:45`) sets the target highlight duration. On Enter or value change, an automatic binary threshold search (12 iterations, backend DRY_RUN) finds the CLIP threshold that produces a film closest to the target. The result — scene count and estimated duration — appears in the counter above the gallery.

Jeśli zadany czas jest nieosiągalny (za mało materiału), wyświetlane jest ostrzeżenie `⚠ max ~m:ss`.

If the target is unreachable (not enough footage), a `⚠ max ~m:ss` warning is shown.

---

## Threshold

Pole **Threshold** (z przyciskami ▼/▲ co 0.001) ustawia próg CLIP ręcznie. Sceny powyżej progu mają pomarańczową ramkę (included), poniżej — szarą (excluded). Zmiana progu ręcznie nadpisuje wynik automatycznego wyszukiwania.

The **Threshold** field (with ▼/▲ buttons stepping 0.001) sets the CLIP threshold manually. Scenes above the threshold have an orange border (included), below — grey (excluded). Manual threshold change overrides the auto-search result.

### Szacowany czas / Duration estimate

Licznik nad galerią (`N / total scenes · m:ss`) pokazuje:

- **Po renderze** — dokładny wynik ostatniego rendera.
- **Po zmianie threshold lub overrides** — estymację z DRY_RUN (dokładny Python, nie aproksymacja JS), aktualizowaną ~1 s po zatrzymaniu suwaka.
- **Dual-camera** — wynik uwzględnia sparowane sceny z drugiej kamery.

The counter above the gallery (`N / total scenes · m:ss`) shows:

- **After render** — exact result of the last render.
- **After threshold/override change** — DRY_RUN estimate (exact Python), updated ~1 s after the slider stops.
- **Dual-camera** — result accounts for paired back-cam scenes.

### Odznaka czasu sceny / Scene duration badge

Pod wynikiem CLIP każdej sceny wyświetlany jest efektywny czas jej udziału w filmie (po zastosowaniu Max scene sec).

Below each scene's CLIP score, the effective clip duration (after applying Max scene sec cap) is shown.

---

## Limit per file

Sceny które przeszły threshold, ale zostały odcięte przez `max_per_file_sec`, oznaczone są bursztynową ramką z plakietką **limit**. Kliknięcie takiej sceny force-include'uje ją (z pominięciem limitu).

Scenes that passed the threshold but were cut by `max_per_file_sec` are shown with an amber border and **limit** badge. Clicking such a scene force-includes it (bypassing the cap).

---

## Manualne overrides / Manual overrides

Kliknięcie klatki przełącza jej status:
- **Included → force-exclude** (ciemna ramka, ikona ×)
- **Excluded → force-include** (zielona ramka, ikona ✓)
- **Manual → reset** (powrót do decyzji threshold)

Overrides zapisywane są po stronie serwera w `_autoframe/manual_overrides.json` i stosowane przy każdym kolejnym renderze. Zmiana Target dur. przelicza threshold z uwzględnieniem aktywnych overrides.

Clicking a frame toggles its status:
- **Included → force-exclude** (dark border, × icon)
- **Excluded → force-include** (green border, ✓ icon)
- **Manual → reset** (back to threshold decision)

Overrides are saved server-side in `_autoframe/manual_overrides.json` and applied on every render. Changing Target dur. re-runs the search with active overrides respected.

---

## Filter

Dwa pola tekstowe filtrują widoczne sceny:

| Pole | Placeholder | Działanie |
|------|-------------|-----------|
| Score | `score` | Prefiks score — np. `0.8` pokazuje sceny 0.800–0.899 |
| Time | `HH:MM` | Prefiks czasu — np. `09` pokazuje sceny z godziny 09:xx |

Wciśnięcie Enter lub opuszczenie pola stosuje filtr. Oba filtry można łączyć.

Two text inputs filter the visible scenes:

| Field | Placeholder | Behaviour |
|-------|-------------|-----------|
| Score | `score` | Score prefix — e.g. `0.8` shows scenes 0.800–0.899 |
| Time | `HH:MM` | Timestamp prefix — e.g. `09` shows scenes with 09:xx timecodes |

Press Enter or blur to apply. Both filters can be combined.

---

## ↺ Reset

Czyści wszystkie manualne overrides i ponownie uruchamia automatyczne wyszukiwanie progu dla bieżącego Target dur. (lub przywraca próg z analizy, jeśli Target dur. nie jest ustawione).

Clears all manual overrides and re-runs the automatic threshold search for the current Target dur. (or restores the analysis threshold if Target dur. is not set).

---

## → Music

Przycisk w prawym górnym rogu galerii przenosi bezpośrednio na zakładkę Music.

Button in the top-right of the gallery switches directly to the Music tab.
