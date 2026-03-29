FROM python:3.12-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash wget xz-utils libsndfile1 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Static ffmpeg build (GPL, with NVENC/NVDEC support)
RUN wget -q -O /tmp/ffmpeg.tar.xz \
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz" \
    && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp \
    && mv /tmp/ffmpeg-master-latest-linux64-gpl/bin/ffmpeg /usr/local/bin/ffmpeg \
    && mv /tmp/ffmpeg-master-latest-linux64-gpl/bin/ffprobe /usr/local/bin/ffprobe \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && rm -rf /tmp/ffmpeg*

WORKDIR /app

# Install Python dependencies (torch from CUDA 12.8 wheel index)
COPY requirements.txt .
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cu128 \
        -r requirements.txt

# Copy application
COPY autoframe.sh clip_score.py select_scenes.py music_index.py \
     generate_config.py gpu_detect.py config.ini ./
COPY webapp/ ./webapp/

# Jobs directory — writable by container user
RUN mkdir -p /app/webapp/jobs && chmod 777 /app/webapp/jobs

EXPOSE 8000

CMD ["uvicorn", "webapp.server:app", "--host", "0.0.0.0", "--port", "8000"]
