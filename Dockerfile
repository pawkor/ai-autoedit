# ubuntu:24.04 — matches host OS; required for jellyfin-ffmpeg7 apt package
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-dev python3-pip \
        wget gnupg ca-certificates \
        bash libsndfile1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# jellyfin-ffmpeg7 — shared build known to work with NVIDIA driver 550+
# (BtbN static build fails with this driver version)
RUN wget -q -O /tmp/jellyfin.gpg https://repo.jellyfin.org/jellyfin_team.gpg.key \
    && gpg --dearmor < /tmp/jellyfin.gpg > /usr/share/keyrings/jellyfin.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/jellyfin.gpg] https://repo.jellyfin.org/ubuntu noble main" \
       > /etc/apt/sources.list.d/jellyfin.list \
    && apt-get update && apt-get install -y --no-install-recommends jellyfin-ffmpeg7 \
    && rm -rf /var/lib/apt/lists/* /tmp/jellyfin.gpg

# jellyfin-ffmpeg on PATH; its bundled libs take priority
ENV PATH="/usr/lib/jellyfin-ffmpeg:${PATH}"
ENV LD_LIBRARY_PATH="/usr/lib/jellyfin-ffmpeg/lib:${LD_LIBRARY_PATH:-}"

WORKDIR /app

# Install heavy ML dependencies first — separate layer so it stays cached
# when requirements.txt changes. Versions must match requirements.txt.
RUN pip install --no-cache-dir --break-system-packages \
        --extra-index-url https://download.pytorch.org/whl/cu128 \
        torch==2.11.0+cu128 torchvision==0.26.0+cu128 open_clip_torch==3.3.0

# Install remaining dependencies (torch already satisfied, skipped by pip)
# Note: decord (gpu_detect.py) is NOT included — requires building from source
# with two patches; --gpudetect is not exposed via the webapp UI anyway.
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages \
        --extra-index-url https://download.pytorch.org/whl/cu128 \
        -r requirements.txt

# Copy application
COPY autoframe.sh clip_score.py select_scenes.py music_index.py \
     generate_config.py gpu_detect.py config.ini ./
COPY webapp/ ./webapp/

# Jobs directory — writable by container user (bind-mounted at runtime)
RUN mkdir -p /app/webapp/jobs && chmod 777 /app/webapp/jobs

EXPOSE 8000

CMD ["python3.12", "-m", "uvicorn", "webapp.server:app", "--host", "0.0.0.0", "--port", "8000"]
