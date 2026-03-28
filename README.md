# autoframe — AI Highlight Reel Pipeline

Każdy dzień na motocyklu to kilkaset gigabajtów surowego materiału z jednej lub dwóch kamer. Ręczny montaż takiej ilości nagrań — przeglądanie, wycinanie, składanie — zajmuje wielokrotnie więcej czasu niż sam wyjazd. Po powrocie z dłuższej trasy czeka kilkanaście dni do obrobienia.

autoframe powstał żeby rozwiązać ten problem. Uruchamiasz skrypt w katalogu dnia — highlight gotowy. Model CLIP ocenia każdą scenę semantycznie (rozumie co jest w kadrze, nie tylko ruch czy jasność), wybiera najlepsze ujęcia, przeplata materiał z dwóch kamer i miesza z muzyką dobraną do charakteru nagrania. Bez ręcznego montażu, bez przeglądania godzin materiału.

Pipeline obsługuje kamerę kaskową (helmet cam) i kamerę 360° Insta360 X2 montowaną na lusterku — łącznie potrafi przetworzyć ponad 700 GB materiału z jednego dnia jazdy.

## Przykładowy film

[![Przykładowy highlight](https://img.youtube.com/vi/kR-plye7V2s/maxresdefault.jpg)](https://www.youtube.com/watch?v=kR-plye7V2s)

## Dokumentacja

- [Jak to działa](docs/how-it-works.md)
- [Instalacja](docs/installation.md)
- [Użycie](docs/usage.md)
- [Konfiguracja](docs/configuration.md)
- [Biblioteka muzyczna](docs/music.md)

---

---

# autoframe — AI Highlight Reel Pipeline

Every day on a motorcycle produces hundreds of gigabytes of raw footage from one or two cameras. Manually editing that volume — reviewing, cutting, assembling — takes many times longer than the ride itself. After a longer trip there are weeks of footage waiting to be processed.

autoframe was built to solve this. Run the script in the day's directory — highlight done. A CLIP model scores each scene semantically (it understands what is in the frame, not just motion or brightness), picks the best shots, interleaves footage from two cameras, and mixes in music matched to the character of the ride. No manual editing, no scrubbing through hours of footage.

The pipeline handles a helmet camera and an Insta360 X2 360° camera mounted on the mirror — capable of processing over 700 GB of footage from a single day of riding.

## Documentation

- [How it works](docs/how-it-works.md)
- [Installation](docs/installation.md)
- [Usage](docs/usage.md)
- [Configuration reference](docs/configuration.md)
- [Music library](docs/music.md)
