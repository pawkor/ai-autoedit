# AI-autoedit — AI Highlight Reel Pipeline

Każdy dzień na motocyklu to kilkaset gigabajtów surowego materiału z jednej lub dwóch kamer. Ręczny montaż zajmuje wielokrotnie więcej czasu niż sam wyjazd.

AI-autoedit rozwiązuje ten problem: model CLIP ocenia każdą scenę semantycznie — rozumie co jest w kadrze, nie tylko ruch czy jasność — i automatycznie składa highlight reel z muzyką. Próg selekcji, manualne overrides klatek i dobór muzyki dostępne są przez przeglądarkę bez dotykania terminala.

---

Every day on a motorcycle produces hundreds of gigabytes of raw footage from one or two cameras. Manual editing takes longer than the ride itself.

AI-autoedit solves this: a CLIP model scores each scene semantically — understanding what's in the frame, not just motion or brightness — and automatically assembles a highlight reel with music. Selection threshold, manual frame overrides, and music selection are all accessible through the browser without touching the terminal.

---

## Przykładowy film / Sample output

[![Przykładowy highlight](https://img.youtube.com/vi/kR-plye7V2s/maxresdefault.jpg)](https://www.youtube.com/watch?v=kR-plye7V2s)

## Dokumentacja / Documentation

### Web UI — kolejność pracy / workflow order

- [Lista projektów i nowy projekt / Project list & New project](docs/ui-projects.md)
- [Settings](docs/ui-settings.md)
- [Gallery](docs/ui-gallery.md)
- [Music](docs/ui-music.md)
- [Summary & Render](docs/ui-summary.md)
- [Log](docs/ui-log.md)
- [Results & YouTube upload](docs/ui-results.md)

### Techniczne / Technical

- [Jak to działa / How it works](docs/how-it-works.md)
- [Instalacja / Installation](docs/installation.md)
- [Konfiguracja / Configuration reference](docs/configuration.md)
- [Biblioteka muzyczna / Music library](docs/music.md)

---

## Licencja / License

**PL:** AGPL-3.0 + Commons Clause — możesz używać, modyfikować i hostować lokalnie za darmo. **Komercyjne użycie i udostępnianie jako usługa (SaaS) są zabronione.** Plik licencji: [LICENSE](LICENSE) (wersja angielska jest wiążąca prawnie).

**EN:** AGPL-3.0 + Commons Clause — free to use, modify, and self-host. **Commercial use and SaaS hosting are prohibited.** See [LICENSE](LICENSE).

## Prywatność / Privacy

**PL:** AI-autoedit działa wyłącznie na Twoim sprzęcie. Pliki wideo nie są przesyłane nigdzie bez Twojej wiedzy.

Zapytania sieciowe wychodzące na zewnątrz:

| Serwis | Co jest wysyłane | Cel | Kiedy |
|--------|-----------------|-----|-------|
| [Anthropic Claude API](https://www.anthropic.com/privacy) | Opis wyjazdu (tekst) | Generowanie promptów CLIP | Po kliknięciu „Generate prompts" |
| [Last.fm API](https://www.last.fm/api/tos) | Nazwa artysty, tytuł utworu | Rozpoznanie gatunku muzycznego | Przy budowaniu indeksu muzycznego |
| YouTube (yt-dlp) | URL utworu | Pobranie audio do biblioteki muzycznej | Po kliknięciu „↓ Download" w zakładce Music |
| Twój bucket S3 *(opcjonalne)* | Pliki wideo źródłowe, wyniki, muzyka | Przechowywanie w chmurze | Tylko gdy S3 skonfigurowane w `.env` |

Brak telemetrii, analityki i wymogu konta. Jeśli nie używasz „Generate prompts", indeksu muzycznego, pobierania z YouTube ani S3, żadne dane nie opuszczają Twojego komputera.

---

**EN:** AI-autoedit runs entirely on your own hardware. Your video files are never sent anywhere without your knowledge.

Outbound network requests:

| Service | What is sent | Purpose | When |
|---------|-------------|---------|------|
| [Anthropic Claude API](https://www.anthropic.com/privacy) | Ride description text | Generate CLIP prompts | When you click "Generate prompts" |
| [Last.fm API](https://www.last.fm/api/tos) | Artist name, track title | Music genre lookup | When building the music index |
| YouTube (yt-dlp) | Track URL | Download audio to music library | When clicking "↓ Download" in the Music tab |
| Your S3 bucket *(optional)* | Source videos, results, music | Cloud storage | Only when S3 is configured in `.env` |

No telemetry, no analytics, no account required. If you do not use "Generate prompts", music genre lookup, YouTube download, or S3, no data leaves your machine.

## Zastrzeżenie / Disclaimer

**PL:** **Zrób kopię zapasową materiału przed użyciem.** Oprogramowanie odczytuje i przetwarza pliki wideo. Autor nie ponosi odpowiedzialności za utratę danych, uszkodzenie plików ani żadne inne szkody wynikające z użycia tego oprogramowania. Pełna klauzula wyłączenia odpowiedzialności: [LICENSE](LICENSE).

**EN:** **Back up your footage before use.** This software reads and processes video files. The author is not responsible for data loss, file corruption, or any other damage arising from use of this software. See [LICENSE](LICENSE) for the full warranty disclaimer.

