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
- [Roadmap funkcji / Feature roadmap](docs/features.md)

---

## Licencja / License

AGPL-3.0 + Commons Clause — możesz używać, modyfikować i hostować lokalnie za darmo. Komercyjne użycie i SaaS są zabronione. → [Licencja / License](docs/license.md)

## Prywatność / Privacy

Działa wyłącznie na Twoim sprzęcie. Brak telemetrii i wymogu konta. → [Prywatność / Privacy](docs/privacy.md)

## Zastrzeżenie / Disclaimer

**PL:** **Zrób kopię zapasową materiału przed użyciem.** Autor nie ponosi odpowiedzialności za utratę danych ani uszkodzenie plików. Pełna klauzula: [LICENSE](LICENSE).

**EN:** **Back up your footage before use.** The author is not responsible for data loss or file corruption. See [LICENSE](LICENSE).

