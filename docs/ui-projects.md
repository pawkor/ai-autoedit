# Lista projektów i nowy projekt / Project list & New project

## Lista projektów / Project list

![Ekran główny](img/AI-autoedit-main-screen.png)

Lewy pasek wyświetla historię zadań z ich statusem (`done` / `running` / `failed`) i czasem trwania. Przełącznik **en / pl** zmienia język interfejsu bez przeładowania strony. Kliknięcie projektu otwiera go i przełącza na zakładkę Summary.

The left sidebar shows job history with status and elapsed time. The **en / pl** switcher changes the interface language without reload. Clicking a project opens it and switches to the Summary tab.

---

## Nowy projekt / New project

![Nowy projekt](img/AI-autoedit-new-project.png)

### Pola formularza / Form fields

| Pole | Opis |
|------|------|
| Work directory | Katalog z plikami MP4 (ścieżka wewnątrz kontenera, np. `/data/2025/04-Grecja/04.21/helmet`) |
| Music directory | Katalog z biblioteką muzyczną (np. `/data/music/NCS`) |
| Cam A / Cam B | Tryb dual-camera: podkatalogi dwóch kamer. Cam A = źródło audio, Cam B = wyciszona. |
| No intro / No music | Pomijają intro/outro lub miks muzyczny. |

### Generowanie promptów CLIP / Generating CLIP prompts

Pole tekstowe **About this ride** + przycisk **Generate CLIP prompts** wywołuje Claude API i generuje prompty POSITIVE/NEGATIVE dopasowane do opisu wyjazdu. Wyniki pojawiają się w polach obok. Przycisk **Save prompts** zapisuje je do `config.ini` projektu.

The **About this ride** text area + **Generate CLIP prompts** button calls Claude API and generates POSITIVE/NEGATIVE prompts matched to the ride description. Results appear in the adjacent fields. **Save prompts** writes them to the project's `config.ini`.

Kliknięcie **▶ Analyze** uruchamia pipeline, automatycznie ustawia wstępny threshold i przenosi na zakładkę Gallery.

Clicking **▶ Analyze** starts the pipeline, auto-sets an initial threshold, and switches to the Gallery tab.
