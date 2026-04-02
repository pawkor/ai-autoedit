# Prywatność / Privacy

## PL

AI-autoedit działa wyłącznie na Twoim sprzęcie. Pliki wideo nie są przesyłane nigdzie bez Twojej wiedzy.

Zapytania sieciowe wychodzące na zewnątrz:

| Serwis | Co jest wysyłane | Cel | Kiedy |
|--------|-----------------|-----|-------|
| [Anthropic Claude API](https://www.anthropic.com/privacy) | Opis wyjazdu (tekst) | Generowanie promptów CLIP | Po kliknięciu „Generate prompts" |
| [Last.fm API](https://www.last.fm/api/tos) | Nazwa artysty, tytuł utworu | Rozpoznanie gatunku muzycznego | Przy budowaniu indeksu muzycznego |
| YouTube (yt-dlp) | URL utworu | Pobranie audio do biblioteki muzycznej | Po kliknięciu „↓ Download" w zakładce Music |
| Twój bucket S3 *(opcjonalne)* | Pliki wideo źródłowe, wyniki, muzyka | Przechowywanie w chmurze | Tylko gdy S3 skonfigurowane w `.env` |

Brak telemetrii, analityki i wymogu konta. Jeśli nie używasz „Generate prompts", indeksu muzycznego, pobierania z YouTube ani S3, żadne dane nie opuszczają Twojego komputera.

---

## EN

AI-autoedit runs entirely on your own hardware. Your video files are never sent anywhere without your knowledge.

Outbound network requests:

| Service | What is sent | Purpose | When |
|---------|-------------|---------|------|
| [Anthropic Claude API](https://www.anthropic.com/privacy) | Ride description text | Generate CLIP prompts | When you click "Generate prompts" |
| [Last.fm API](https://www.last.fm/api/tos) | Artist name, track title | Music genre lookup | When building the music index |
| YouTube (yt-dlp) | Track URL | Download audio to music library | When clicking "↓ Download" in the Music tab |
| Your S3 bucket *(optional)* | Source videos, results, music | Cloud storage | Only when S3 is configured in `.env` |

No telemetry, no analytics, no account required. If you do not use "Generate prompts", music genre lookup, YouTube download, or S3, no data leaves your machine.
