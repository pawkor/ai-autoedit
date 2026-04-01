# Zakładka Log / Log tab

![Log](img/AI-autoedit-log.png)

Zakładka **Log** pokazuje pełny output pipeline w czasie rzeczywistym przez WebSocket. Widoczne są wszystkie kroki: detekcja, scoring CLIP, selekcja scen, enkodowanie. Pasek postępu enkodowania aktualizuje się na bieżąco z ETA.

The **Log** tab shows full pipeline output in real time via WebSocket. All steps are visible: detection, CLIP scoring, scene selection, encoding. The encoding progress bar updates continuously with ETA.

Po prawej stronie wykresy zasobów systemowych aktualizowane co sekundę: CPU, RAM, GPU, VRAM oraz kolejka zadań.

On the right: system resource graphs updated every second — CPU, RAM, GPU, VRAM — and the job queue.

## ■ Stop job

Przycisk **■ Stop job** widoczny nad logiem gdy job jest uruchomiony. Wysyła SIGTERM do procesu — job zatrzymuje się, pliki częściowe pozostają na dysku (można wznowić przez Re-analyze).

The **■ Stop job** button appears above the log while a job is running. Sends SIGTERM to the process — the job stops, partial files remain on disk (can be resumed via Re-analyze).
