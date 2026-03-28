# Instalacja / Installation

## PL

### Wymagania systemowe

- Ubuntu 24.04 LTS
- GPU NVIDIA z CUDA (testowane: RTX 3070 Ti, driver 550)
- Python 3.12
- ~4 GB VRAM (model ViT-L-14)

### 1. Pakiety systemowe

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip rename cmake build-essential git
```

### 2. ffmpeg z NVENC

Standardowy pakiet Ubuntu zazwyczaj nie ma NVENC — użyj jellyfin-ffmpeg:

```bash
curl -fsSL https://repo.jellyfin.org/ubuntu/jellyfin_team.gpg.key \
    | sudo gpg --dearmor -o /usr/share/keyrings/jellyfin.gpg
echo "deb [signed-by=/usr/share/keyrings/jellyfin.gpg] https://repo.jellyfin.org/ubuntu jammy main" \
    | sudo tee /etc/apt/sources.list.d/jellyfin.list
sudo apt update && sudo apt install -y jellyfin-ffmpeg7
```

Po instalacji ustaw w `config.ini`:
```ini
[paths]
ffmpeg  = /usr/lib/jellyfin-ffmpeg/ffmpeg
ffprobe = /usr/lib/jellyfin-ffmpeg/ffprobe
```

Weryfikacja:
```bash
/usr/lib/jellyfin-ffmpeg/ffmpeg -encoders 2>/dev/null | grep nvenc
```

Alternatywnie: zwykły ffmpeg z apt działa jeśli NVENC nie jest potrzebny (fallback na libx264).

### 3. Klonowanie repo

```bash
git clone https://github.com/pawkor/ai-autoedit ~/ai-autoedit
```

### 4. Python venv

```bash
python3 -m venv ~/highlight-env
source ~/highlight-env/bin/activate
```

### 5. PyTorch z CUDA

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
```

Weryfikacja:
```bash
python3 -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### 6. Pakiety Python

```bash
pip install \
    open-clip-torch==3.3.0 \
    scenedetect[opencv] \
    librosa==0.11.0 \
    pandas \
    Pillow \
    numpy \
    tqdm \
    soundfile \
    anthropic
```

### 7. decord z obsługą CUDA (opcjonalnie, ~2× szybsza detekcja)

`decord` z PyPI dekoduje wideo na CPU. Żeby używać NVDEC, trzeba skompilować ze źródeł. `decord` nie był aktualizowany od lat i wymaga patchy dla FFmpeg 6+/7+.

```bash
git clone --recursive https://github.com/dmlc/decord ~/decord
cd ~/decord && mkdir build && cd build

# Patch 1: brakujący include bsf.h (FFmpeg 6+)
sed -i '/#include <libavcodec\/avcodec.h>/a #include <libavcodec\/bsf.h>' \
    ~/decord/src/video/ffmpeg/ffmpeg_common.h

# Patch 2: wyłączenie audio (niezgodne API channel_layout w FFmpeg 7.x)
sed -i 's|src/\*.cc src/runtime/\*.cc src/video/\*.cc src/sampler/\*.cc src/audio/\*.cc src/av_wrapper/\*.cc|src/*.cc src/runtime/*.cc src/video/*.cc src/sampler/*.cc src/av_wrapper/*.cc|' \
    ~/decord/CMakeLists.txt

# Kompilacja (86 = RTX 30xx, 89 = RTX 40xx, 120 = RTX 50xx)
CUDACXX=/usr/local/cuda-12.8/bin/nvcc cmake .. \
    -DUSE_CUDA=ON \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CUDA_ARCHITECTURES=86 \
    -DCMAKE_CXX_FLAGS="-fpermissive"
make -j$(nproc)

# Instalacja
source ~/highlight-env/bin/activate
pip install -e ~/decord/python/
```

Weryfikacja:
```bash
python3 -c "import decord; decord.gpu(0); print('decord CUDA OK')"
```

Bez CUDA build pipeline działa z CPU fallback — detekcja wolniejsza ale poprawna.

### 8. Font

```bash
mkdir -p ~/fonts
# Pobierz Caveat-Bold.ttf i umieść w ~/fonts/
# https://fonts.google.com/specimen/Caveat
```

---

## EN

### System requirements

- Ubuntu 24.04 LTS
- NVIDIA GPU with CUDA (tested: RTX 3070 Ti, driver 550)
- Python 3.12
- ~4 GB VRAM (ViT-L-14 model)

### 1. System packages

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip rename cmake build-essential git
```

### 2. ffmpeg with NVENC

The standard Ubuntu package typically lacks NVENC — use jellyfin-ffmpeg:

```bash
curl -fsSL https://repo.jellyfin.org/ubuntu/jellyfin_team.gpg.key \
    | sudo gpg --dearmor -o /usr/share/keyrings/jellyfin.gpg
echo "deb [signed-by=/usr/share/keyrings/jellyfin.gpg] https://repo.jellyfin.org/ubuntu jammy main" \
    | sudo tee /etc/apt/sources.list.d/jellyfin.list
sudo apt update && sudo apt install -y jellyfin-ffmpeg7
```

Then set in `config.ini`:
```ini
[paths]
ffmpeg  = /usr/lib/jellyfin-ffmpeg/ffmpeg
ffprobe = /usr/lib/jellyfin-ffmpeg/ffprobe
```

### 3. Clone repo

```bash
git clone https://github.com/pawkor/ai-autoedit ~/ai-autoedit
```

### 4. Python venv

```bash
python3 -m venv ~/highlight-env
source ~/highlight-env/bin/activate
```

### 5. PyTorch with CUDA

```bash
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
```

### 6. Python packages

```bash
pip install \
    open-clip-torch==3.3.0 \
    scenedetect[opencv] \
    librosa==0.11.0 \
    pandas \
    Pillow \
    numpy \
    tqdm \
    soundfile \
    anthropic
```

### 7. decord with CUDA support (optional, ~2× faster detection)

`decord` from PyPI decodes video on CPU. Building from source enables NVDEC hardware decoding. The project requires patches for FFmpeg 6+/7+ API incompatibilities.

```bash
git clone --recursive https://github.com/dmlc/decord ~/decord
cd ~/decord && mkdir build && cd build

# Patch 1: missing bsf.h include (FFmpeg 6+)
sed -i '/#include <libavcodec\/avcodec.h>/a #include <libavcodec\/bsf.h>' \
    ~/decord/src/video/ffmpeg/ffmpeg_common.h

# Patch 2: disable audio (channel_layout API changed in FFmpeg 7.x)
sed -i 's|src/\*.cc src/runtime/\*.cc src/video/\*.cc src/sampler/\*.cc src/audio/\*.cc src/av_wrapper/\*.cc|src/*.cc src/runtime/*.cc src/video/*.cc src/sampler/*.cc src/av_wrapper/*.cc|' \
    ~/decord/CMakeLists.txt

# Build (86 = RTX 30xx, 89 = RTX 40xx, 120 = RTX 50xx)
CUDACXX=/usr/local/cuda-12.8/bin/nvcc cmake .. \
    -DUSE_CUDA=ON \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CUDA_ARCHITECTURES=86 \
    -DCMAKE_CXX_FLAGS="-fpermissive"
make -j$(nproc)

source ~/highlight-env/bin/activate
pip install -e ~/decord/python/
```

Verify:
```bash
python3 -c "import decord; decord.gpu(0); print('decord CUDA OK')"
```

Without the CUDA build the pipeline falls back to CPU decoding — slower but correct.

### 8. Font

```bash
mkdir -p ~/fonts
# Download Caveat-Bold.ttf and place it in ~/fonts/
# https://fonts.google.com/specimen/Caveat
```
