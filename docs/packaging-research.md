# Pakowanie ai-autoedit jako standalone .exe / .app

*Research: 2026-06-09*

## Dlaczego pakowanie jest trudne

Stack ma wyjątkowo ciężki profil do pakowania:

- **PyTorch CUDA 12.8** — wheel `cu128` to ~2.8 GB torch+torchvision
- **open_clip + ViT-L-14** — ~900 MB model, pobierany przy pierwszym uruchomieniu
- **EasyOCR + YOLO** — kolejne modele (~1.5 GB po inicjalizacji)
- **jellyfin-ffmpeg7** — niestandardowy build pod Ubuntu z APT, **nie istnieje na Windows ani macOS**
- **librosa** — wymaga `libsndfile`, `libgomp` (C libraries)

Łączna wielkość środowiska: **~8–12 GB** (CUDA), **~3–4 GB** (CPU-only).  
GitHub Releases limit = 2 GB — dystrybucja jako jeden plik bez własnego CDN odpada.

Standard w tej klasie aplikacji (Ollama, LM Studio, ComfyUI): mały launcher + modele pobierane przy pierwszym uruchomieniu.

---

## Analiza per platforma

### Windows (.exe)

**CUDA**: dostępna jeśli użytkownik ma sterownik NVIDIA. `torch+cu128` wheel z pytorch.org działa. CUDA runtime DLL można redystrybuować (~500 MB) lub wymagać zainstalowanego sterownika.

**ffmpeg**: jellyfin-ffmpeg7 odpada. Zamiennik: statyczny build z BtbN (`ffmpeg-master-latest-win64-gpl.zip`, ~120 MB). NVENC działa przez sterownik NVIDIA — brak dodatkowych wymagań.

**Narzędzia pakujące**:
- PyInstaller / cx_Freeze — technicznie działają z torch, ale wymagają ręcznych hooków dla torch/open_clip/easyocr/ultralytics. Efekt: folder ~15–25 GB, znane problemy z CUDA DLL discovery
- Nuitka — nie obsługuje dobrze natywnych rozszerzeń PyTorch (JIT, custom C++ extensions)
- **conda-pack + Inno Setup** — najrealistyczniejsze: pakuje całe środowisko conda bez hooków, opakowuje jako installer. Rozmiar: ~1.5 GB (CPU-only), ~4–6 GB (CUDA)

**Podpisywanie**: bez EV cert (~300–500 USD/rok) pojawi się SmartScreen "Windows protected your PC". Obejście dla użytkownika: "Więcej informacji → Uruchom mimo to". MSIX przez Microsoft Partner Center (darmowe, ale wielotygodniowy proces).

**Feasibility**: Wysoka. Wysiłek: 4–8 tygodni.

---

### macOS Apple Silicon — M1/M2/M3/M4

**GPU**: PyTorch MPS backend (Metal Performance Shaders) od PyTorch 1.13. open_clip obsługuje MPS. CLIP scoring na M2 Pro: **~3–8 minut** dla 300 scen — porównywalnie z entry-level NVIDIA GPU. Akceptowalne.

**ffmpeg**: jellyfin-ffmpeg7 odpada. Zamiennik: statyczny build arm64 z evermeet.cx lub `brew install ffmpeg`. Encoding przez VideoToolbox (Apple hardware encoder, szybki dla 4K HEVC). Wymaga warunkowego wykrycia platformy w kodzie (zamiast hardcoded NVENC params).

**Narzędzia**: conda-pack z miniforge (arm64). py2app/Briefcase mają te same problemy co PyInstaller z torch. Launcher: `.command` file (double-clickable bash) lub Platypus wrapper.

**Rozmiar**: torch arm64 CPU/MPS ~700 MB. Całe środowisko: ~2–3 GB. Instalator po kompresji: ~800 MB–1.2 GB.

**Podpisywanie**: Apple Developer Program = 99 USD/rok. Bez certyfikatu: Gatekeeper blokuje przy pierwszym uruchomieniu → right-click → Open → potwierdzenie. Dla power userów — do zaakceptowania.

**Feasibility**: Średnia-wysoka. Najlepszy cel macOS. Wysiłek: 3–5 tygodni.

---

### macOS Intel

**Problem**: Apple usunęło CUDA w 2019. CLIP scoring na CPU (Core i7/i9): **45–90 minut** dla 300 scen. Nie do zaakceptowania jako produkt.

**Verdict**: Pomijalne. Użytkownicy z Intel Mac mogą korzystać z Docker lub poczekać na migrację do Apple Silicon.

---

## Rekomendowane podejście — trzy kroki

### Krok 1: install.bat + install.sh *(3–7 dni)*

Najszybsze odblokowanie użytkowników bez Dockera. Skrypt (~50 KB) pobiera zależności przy pierwszym uruchomieniu.

**Windows — install.bat:**
```
1. Sprawdź Python 3.12 → jeśli brak, pobierz python.org embeddable pack
2. Utwórz venv w %APPDATA%\ai-autoedit\venv
3. pip install -r requirements.txt (CPU-only torch jako default)
4. Pobierz statyczny ffmpeg do %APPDATA%\ai-autoedit\bin\
5. Utwórz ai-autoedit.bat launcher + skrót na pulpicie
```

**macOS — install.sh:**
```
1. Sprawdź brew → jeśli brak, zainstaluj (wymaga potwierdzenia usera)
2. brew install python@3.12 ffmpeg
3. python3.12 -m venv ~/.local/share/ai-autoedit/venv
4. pip install -r requirements.txt
5. Utwórz .command launcher (double-clickable)
```

Modele (ViT-L-14, YOLO) pobierane przy pierwszym uruchomieniu — pasek postępu już częściowo zaimplementowany przez SSE w webapp.

---

### Krok 2: mały launcher GUI *(2–4 tygodnie)*

~5 MB exe/app napisany w **Go** lub **Rust** (zero własnych zależności ML):

- Sprawdza czy środowisko zainstalowane w `%APPDATA%\ai-autoedit\` / `~/.local/share/ai-autoedit/`
- Przy pierwszym uruchomieniu: wywołuje install.bat/sh z paskiem postępu w okienku
- Uruchamia uvicorn jako subprocess
- Otwiera `http://localhost:8000` w przeglądarce
- Ikona w system tray z przyciskiem "Stop Server"

Wzorce tego podejścia: **Ollama**, **LM Studio**, **LocalAI**, **ComfyUI Desktop**.

---

### Krok 3: conda-pack + Inno Setup / DMG *(2–3 miesiące)*

Pełny bundled installer — zero zależności zewnętrznych, działa na czystej maszynie.

- Windows: conda-pack → Inno Setup `.exe`, opcjonalne wykrycie CUDA przy instalacji i doinstalowanie `torch+cu128`
- macOS: conda-pack (miniforge arm64) → DMG z `.app` (Platypus lub Briefcase)

Uzasadnione jeśli projekt zyska wystarczającą trakcję. Na tym etapie — over-engineering.

---

## Podsumowanie

| Platforma | Feasibility | Główny bloker | Wysiłek |
|---|---|---|---|
| Windows CUDA | Wysoka | Rozmiar dystrybucji, SmartScreen | 4–8 tygodni (Krok 3) |
| Windows CPU-only | Wysoka | CLIP scoring ~45 min na CPU | 3–6 tygodni (Krok 3) |
| macOS Apple Silicon | Średnia-wysoka | Brak jellyfin-ffmpeg, MPS zamiast CUDA | 3–5 tygodni (Krok 3) |
| macOS Intel | Niska | CLIP CPU ~godzina, nieakceptowalne | Pomijalne |
| install.bat/sh | Wysoka | UX nie-technicznych użytkowników | **3–7 dni → zacznij tutaj** |

**Priorytet**: Krok 1 (install scripts) → Krok 2 (launcher GUI) → Krok 3 (full bundle) jeśli uzasadnione.
