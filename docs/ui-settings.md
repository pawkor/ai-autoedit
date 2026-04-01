# Zakładka Settings / Settings tab

![Ustawienia](img/AI-autoedit-settings.png)

Zakładka **Settings** pozwala zmieniać wszystkie parametry pipeline bez edytowania plików. Każde pole zapisuje się do `config.ini` projektu automatycznie po opuszczeniu pola (Enter lub kliknięcie gdzie indziej).

The **Settings** tab lets you change all pipeline parameters without editing files. Every field saves to the project's `config.ini` automatically on blur or Enter.

---

## Sekcje / Sections

### Sources / Working directory

Katalog roboczy z plikami MP4 oraz opcjonalna lista kamer (dla dual-camera).

Working directory with MP4 files and optional camera list (for dual-camera).

### Scene detection *(requires Re-analyze)*

Parametry PySceneDetect — wpływają na etap detekcji cięć. Zmiana wymaga Re-analyze żeby przeliczyć sceny od nowa.

PySceneDetect parameters — affect the cut detection stage. Changes require Re-analyze to reprocess scenes.

| Parametr | Domyślnie | Opis / Description |
|----------|-----------|---------------------|
| Detect threshold | `20` | Czułość detektora. Wyższy = mniej cięć. Dla leśnego materiału zalecane 24–28. / Detector sensitivity. Higher = fewer cuts. For forest/lighting-heavy footage try 24–28. |
| Min scene len | `8` | Minimalna długość sceny w sekundach. / Minimum scene duration in seconds. |

### Scene selection

| Parametr | Opis / Description |
|----------|--------------------|
| Max scene sec | Maks. czas wycinany z jednej sceny (środek klipu). / Max seconds taken per scene (centred). |
| Max per file sec | Maks. łączny czas z jednego pliku. Nadmiarowe sceny oznaczone jako „limit" w Gallery. / Max total seconds from one source file. Excess scenes shown as "limit" in Gallery. |
| Target min | Docelowy czas highlight w minutach — używany przez przycisk ⟳ Fill. / Target highlight duration in minutes — used by the ⟳ Fill button. |

Przycisk **⟳ Fill** oblicza Max scene sec i Max per file sec automatycznie na podstawie liczby plików źródłowych i docelowego czasu.

The **⟳ Fill** button auto-calculates Max scene sec and Max per file sec from the source file count and target duration.

Threshold CLIP ustawiany jest na żywo przez suwak w zakładce Gallery.

The CLIP threshold is set live via the Gallery slider — not in Settings.

### CLIP prompts / About this ride

Opis wyjazdu do generowania promptów przez Claude API. Szczegóły: [Nowy projekt](ui-projects.md).

Ride description for Claude-based prompt generation. Details: [New project](ui-projects.md).

### POSITIVE / NEGATIVE prompts

Edytowalne bezpośrednio, jeden prompt na linię. Zapisywane automatycznie po opuszczeniu pola.

Editable directly, one prompt per line. Saved automatically on blur.

### CLIP scoring

| Parametr | Domyślnie | Opis / Description |
|----------|-----------|--------------------|
| Batch size | `64` | Klatki przez GPU jednocześnie. Obniż do 32/16 przy błędach OOM. / Frames per GPU batch. Lower to 32/16 if OOM errors occur. |
| Workers | `4` | Wątki ładowania klatek. / Frame-loading worker threads. |

Wartości zapisywane automatycznie po opuszczeniu pola. Stosowane przy kolejnym Re-analyze.

Values saved automatically on blur. Applied on the next Re-analyze run.

---

## Re-analyze with these settings

Uruchamia pipeline od nowa. Pomija detekcję scen jeśli pliki źródłowe i parametry `[scene_detection]` nie zmieniły się.

Reruns the pipeline. Skips scene detection if source files and `[scene_detection]` parameters haven't changed.
