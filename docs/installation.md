# Instalacja / Installation

## PL

### Wymagania

- Docker z NVIDIA Container Toolkit
- GPU NVIDIA z CUDA (testowane: RTX 3070 Ti, driver 550)
- ~5 GB VRAM (model ViT-L-14)
- `docker compose` (v2)

### 1. Klonowanie repo

```bash
git clone https://github.com/pawkor/ai-autoedit ~/ai-autoedit
cd ~/ai-autoedit
```

### 2. Zmienne środowiskowe

```bash
cp .env.example .env
```

Edytuj `.env`:

```env
DATA_DIR=/home/user/moto          # katalog z materiałem i muzyką — montowany jako /data w kontenerze
UID=1000                          # twój UID (id -u)
GID=1000                          # twój GID (id -g)
ANTHROPIC_API_KEY=sk-ant-...      # opcjonalnie — do generowania promptów CLIP
LAST_FM_API_KEY=...               # opcjonalnie — do enrichmentu gatunków muzycznych
```

### 3. Uruchomienie

```bash
docker compose up -d
```

Webapp dostępna pod: **http://0.0.0.0:8000**

### 4. Aktualizacja

`webapp/` i `src/` są montowane na żywo — zmiany w HTML/JS/Python działają bez rebuildu obrazu. Jedynie zmiany w `Dockerfile` lub `requirements.txt` wymagają rebuildu:

```bash
docker compose build && docker compose up -d
```

---

### Konfiguracja ffmpeg

Domyślnie kontener używa `jellyfin-ffmpeg` z NVENC. Ścieżka ustawiona w `config.ini`:

```ini
[paths]
ffmpeg  = /usr/lib/jellyfin-ffmpeg/ffmpeg
ffprobe = /usr/lib/jellyfin-ffmpeg/ffprobe
```

Weryfikacja NVENC wewnątrz kontenera:

```bash
docker exec scripts-autoframe-1 /usr/lib/jellyfin-ffmpeg/ffmpeg -encoders 2>/dev/null | grep nvenc
```

---

### AppArmor na Proxmox LXC

Docker wewnątrz Proxmox LXC może blokować operacje `docker build` i startowanie kontenerów z powodu AppArmor. Rozwiązanie: `security_opt: [apparmor:unconfined]` w `docker-compose.yml` (już ustawione).

---

## EN

### Requirements

- Docker with NVIDIA Container Toolkit
- NVIDIA GPU with CUDA (tested: RTX 3070 Ti, driver 550)
- ~5 GB VRAM (ViT-L-14 model)
- `docker compose` (v2)

### 1. Clone

```bash
git clone https://github.com/pawkor/ai-autoedit ~/ai-autoedit
cd ~/ai-autoedit
```

### 2. Environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
DATA_DIR=/home/user/moto          # footage and music root — mounted as /data in the container
UID=1000                          # your UID (id -u)
GID=1000                          # your GID (id -g)
ANTHROPIC_API_KEY=sk-ant-...      # optional — for CLIP prompt generation
LAST_FM_API_KEY=...               # optional — for music genre enrichment via Last.fm
```

### 3. Start

```bash
docker compose up -d
```

Webapp available at: **http://0.0.0.0:8000**

### 4. Updates

`webapp/` and `src/` are live-mounted — HTML/JS/Python changes take effect without rebuilding the image. Only `Dockerfile` or `requirements.txt` changes need a rebuild:

```bash
docker compose build && docker compose up -d
```

---

### ffmpeg configuration

The container uses `jellyfin-ffmpeg` with NVENC by default. Path set in `config.ini`:

```ini
[paths]
ffmpeg  = /usr/lib/jellyfin-ffmpeg/ffmpeg
ffprobe = /usr/lib/jellyfin-ffmpeg/ffprobe
```

Verify NVENC inside the container:

```bash
docker exec scripts-autoframe-1 /usr/lib/jellyfin-ffmpeg/ffmpeg -encoders 2>/dev/null | grep nvenc
```
