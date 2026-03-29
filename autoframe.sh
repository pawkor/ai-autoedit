#!/bin/bash
# autoframe.sh — automatic highlight reel from a day's footage
# Usage: cd /path/to/day && /path/to/repo/autoframe.sh [options]
#
# Options:
#   --threshold N        CLIP score threshold (default: 0.148)
#   --max-scene N        Max seconds per scene clip (default: 10)
#   --per-file N         Max seconds per source file (default: 45)
#   --title "TEXT"       Title card text, use \n for newline (default: auto)
#   --font /path/to.ttf  Font file (default: ~/fonts/Caveat-Bold.ttf)
#   --no-intro           Skip intro/outro
#   --music /path        Directory with MP3 files (default: ~/moto/music)
#   --music-volume N     Music volume 0-1 (default: 0.7)
#   --original-volume N  Original audio volume 0-1 (default: 0.3)
#   --cam-a DIR          Subfolder name for camera A, e.g. "helmet"
#   --cam-b DIR          Subfolder name for camera B, e.g. "mirror"
#   --music-genre GENRE  Filter by genre, e.g. "house", "rock" (default: any)
#   --music-artist NAME  Filter by artist(s), e.g. "elektronomia" or "tobu,alan,janji"
#   --music-rebuild      Rebuild music index and exit
#   --no-music           Skip music mixing
#   --gpudetect          GPU scene detection via decord (default: scenedetect CPU)
#   --about "TEXT"       Describe the day's ride — auto-generates config.ini via Claude API
#   --help

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Config reader ─────────────────────────────────────────────────────────────
cfg() {
    local section="$1" key="$2" default="${3:-}"
    local val _f
    for _f in "$SCRIPT_DIR/config.ini" "$(pwd)/config.ini"; do
        [ -f "$_f" ] || continue
        local _v
        _v=$(awk -v sec="$section" -v k="$key" '
            /^\[/{in_s=($0=="["sec"]")}
            in_s && $0 ~ "^"k"[[:space:]]*=" {
                sub(/^[^=]*=[[:space:]]*/,"")
                sub(/[[:space:]]*#.*/,"")
                sub(/[[:space:]]+$/,"")
                print; exit
            }
        ' "$_f" 2>/dev/null)
        [ -n "$_v" ] && val="$_v"
    done
    echo "${val:-$default}"
}

if [ $# -eq 0 ]; then
    grep '^#' "$0" | head -20 | sed 's/^# \{0,2\}//'
    exit 0
fi

# ── Defaults (from config.ini, overridden by CLI flags) ───────────────────────
THRESHOLD=$(cfg scene_selection threshold "0.148")
THRESHOLD_EXPLICIT=0
MAX_SCENE=$(cfg scene_selection max_scene_sec "10")
PER_FILE=$(cfg scene_selection max_per_file_sec "45")
TITLE=""
MUSIC_DIR=$(eval echo "$(cfg music dir "$HOME/moto/music")")
MUSIC_VOL=$(cfg music music_volume "0.7")
ORIG_VOL=$(cfg music original_volume "0.3")
NO_MUSIC=0
MUSIC_GENRE=""
MUSIC_ARTIST=""
CAM_A=""
CAM_B=""
FONT=$(eval echo "$(cfg intro_outro font "$HOME/fonts/Caveat-Bold.ttf")")
NO_INTRO=0
WORKDIR="$(pwd)"
WORK_SUBDIR=$(cfg paths work_subdir "_autoframe")
VENV=$(eval echo "$(cfg paths venv "$HOME/highlight-env")")
FFMPEG=$(eval echo "$(cfg paths ffmpeg "ffmpeg")")
FFPROBE=$(eval echo "$(cfg paths ffprobe "ffprobe")")

# Video
RESOLUTION=$(cfg video resolution "3840:2160")
FRAMERATE=$(cfg video framerate "60")
AUDIO_BITRATE=$(cfg video audio_bitrate "192k")
NVENC_CQ=$(cfg video nvenc_cq "18")
NVENC_PRESET=$(cfg video nvenc_preset "p4")
X264_CRF=$(cfg video x264_crf "18")
X264_PRESET=$(cfg video x264_preset "fast")

# Scene detection
SD_THRESHOLD=$(cfg scene_detection threshold "20")
SD_MIN_SCENE=$(cfg scene_detection min_scene_len "8s")
GPU_SD_THRESHOLD=$(cfg scene_detection gpu_threshold "30")
GPU_DETECT=0
ABOUT=""

# Intro/outro
INTRO_DURATION=$(cfg intro_outro duration "3")
FADE_DUR=$(cfg intro_outro fade_duration "1")
OUTRO_TEXT=$(cfg intro_outro outro_text "Editing powered by AI")
FONT_SIZE_TITLE=$(cfg intro_outro font_size_title "120")
FONT_SIZE_SUBTITLE=$(cfg intro_outro font_size_subtitle "96")
FONT_SIZE_OUTRO=$(cfg intro_outro font_size_outro "60")

# Music
MUSIC_FADE_DUR=$(cfg music fade_out_duration "3")

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --threshold) THRESHOLD="$2"; THRESHOLD_EXPLICIT=1; shift 2 ;;
        --max-scene) MAX_SCENE="$2";  shift 2 ;;
        --per-file)  PER_FILE="$2";   shift 2 ;;
        --title)     TITLE="$2";      shift 2 ;;
        --font)      FONT="$2";       shift 2 ;;
        --no-intro)       NO_INTRO=1;        shift ;;
        --music)          MUSIC_DIR="$2";    shift 2 ;;
        --music-volume)   MUSIC_VOL="$2";    shift 2 ;;
        --original-volume) ORIG_VOL="$2";   shift 2 ;;
        --cam-a)          CAM_A="$2";         shift 2 ;;
        --cam-b)          CAM_B="$2";         shift 2 ;;
        --music-genre)    MUSIC_GENRE="$2";   shift 2 ;;
        --music-artist)   MUSIC_ARTIST="$2"; shift 2 ;;
        --music-rebuild)
            source "$VENV/bin/activate"
            echo "Rebuilding music index: $MUSIC_DIR"
            python3 "$SCRIPT_DIR"/music_index.py "$MUSIC_DIR" --force
            exit 0 ;;
        --no-music)       NO_MUSIC=1;        shift ;;
        --gpudetect)      GPU_DETECT=1;      shift ;;
        --about)          ABOUT="$2";        shift 2 ;;
        --help)
            grep '^#' "$0" | head -15 | sed 's/^# \{0,2\}//'
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Auto config from description ──────────────────────────────────────────────
if [ -n "$ABOUT" ]; then
    echo "Generating config.ini from description..."
    python3 "$SCRIPT_DIR"/generate_config.py "$ABOUT"
    echo ""
fi

# ── Auto title ────────────────────────────────────────────────────────────────
if [ -z "$TITLE" ]; then
    YEAR=$(echo "$WORKDIR" | grep -oP '\d{4}' | head -1)
    TRIP=$(echo "$WORKDIR" | grep -oP '(?<=\d{4}/)[^/]+' | head -1 \
        | sed 's/[0-9.-]*//g' | tr '-' ' ' | xargs)
    TITLE="${YEAR}\n${TRIP}"
fi

# ── Setup ─────────────────────────────────────────────────────────────────────
AUTODIR="$WORKDIR/$WORK_SUBDIR"
mkdir -p "$AUTODIR"/{autocut,frames,csv,trimmed}

source "$VENV/bin/activate"

# ── GPU / encoder detection ───────────────────────────────────────────────────
if { $FFMPEG -encoders 2>/dev/null || true; } | grep -q h264_nvenc; then
    VID_CODEC="h264_nvenc"
    VID_QUALITY="-rc vbr -cq $NVENC_CQ -b:v 0 -preset $NVENC_PRESET"
    HWACCEL="-hwaccel cuda"
else
    VID_CODEC="libx264"
    VID_QUALITY="-crf $X264_CRF -preset $X264_PRESET"
    HWACCEL=""
fi

PIPELINE_START=$SECONDS

echo "╔══════════════════════════════════════╗"
echo "║         autoframe.sh pipeline        ║"
echo "╠══════════════════════════════════════╣"
printf "║ %-36s ║\n" "Threshold: $THRESHOLD"
printf "║ %-36s ║\n" "Max scene: ${MAX_SCENE}s"
printf "║ %-36s ║\n" "Per file:  ${PER_FILE}s"
printf "║ %-36s ║\n" "Title:     $(echo -e "$TITLE" | head -1)"
echo "╚══════════════════════════════════════╝"
echo ""

# ── Find source files ─────────────────────────────────────────────────────────
CAM_B_PATH="${CAM_B:+$WORKDIR/$CAM_B}"
if [ -n "$CAM_A" ] && [ -n "$CAM_B" ]; then
    mapfile -t SOURCE_FILES < <(
        find "$WORKDIR/$CAM_A" "${CAM_B_PATH:-$WORKDIR/$CAM_B}" \
            -maxdepth 1 -name "*.mp4" ! -iname "*.lrv" \
            | sort
    )
else
    mapfile -t SOURCE_FILES < <(
        find "$WORKDIR" \
            -maxdepth 1 -name "*.mp4" \
            -not -name "highlight*.mp4" \
            ! -iname "*.lrv" \
            | sort
    )
fi

echo "[1/6] Found ${#SOURCE_FILES[@]} source files"

# Build camera_sources.csv if dual-cam mode
if [ -n "$CAM_A" ] && [ -n "$CAM_B" ]; then
    echo "source,camera" > "$AUTODIR/camera_sources.csv"
    for f in "${SOURCE_FILES[@]}"; do
        base=$(basename "${f%.mp4}")
        if [[ "$f" == *"/$CAM_A/"* ]]; then
            echo "${base},${CAM_A}" >> "$AUTODIR/camera_sources.csv"
        elif [[ "$f" == *"/$CAM_B/"* ]]; then
            echo "${base},${CAM_B}" >> "$AUTODIR/camera_sources.csv"
        fi
    done
    echo "  Dual-cam: $CAM_A / $CAM_B"
fi

if [ ${#SOURCE_FILES[@]} -eq 0 ]; then
    echo "No MP4 files found in $WORKDIR"
    echo ""
    grep '^#' "$0" | head -20 | sed 's/^# \{0,2\}//'
    exit 1
fi

# ── Scene Detection + Split ───────────────────────────────────────────────────
echo ""

_TOTAL_FILES=${#SOURCE_FILES[@]}

if [ "$GPU_DETECT" -eq 1 ]; then
    echo "[2-3/6] Scene detection + split (GPU, decord)..."
    _i=0
    for f in "${SOURCE_FILES[@]}"; do
        _i=$((_i + 1))
        echo "  [${_i}/${_TOTAL_FILES}] $(basename "$f")"
        python3 "$SCRIPT_DIR"/gpu_detect.py "$f" \
            "$AUTODIR/csv/" "$AUTODIR/autocut/" \
            --threshold "$GPU_SD_THRESHOLD" \
            --min-scene-len "${SD_MIN_SCENE%s}"
    done
else
    echo "[2/6] Scene detection..."
    PIDS=()
    _i=0
    for f in "${SOURCE_FILES[@]}"; do
        base=$(basename "${f%.mp4}")
        csv="$AUTODIR/csv/${base}-Scenes.csv"

        if [ -f "$csv" ]; then
            _i=$((_i + 1))
            echo "  [${_i}/${_TOTAL_FILES}] ✓ $base (cached)"
            continue
        fi

        _i=$((_i + 1))
        echo "  [${_i}/${_TOTAL_FILES}] → $base"
        (
            scenedetect -f 3 -i "$f" \
                detect-content --threshold "$SD_THRESHOLD" --min-scene-len "$SD_MIN_SCENE" \
                list-scenes -o "$AUTODIR/csv/" \
                2>/dev/null || true
            [ -f "$csv" ] \
                && echo "    $base: $(tail -n +3 "$csv" | wc -l) scenes" \
                || echo "    $base: no scenes detected"
        ) &
        PIDS+=($!)
    done
    for pid in "${PIDS[@]}"; do wait "$pid" || true; done

    echo ""
    echo "[3/6] Splitting scenes..."
    PIDS=()
    for f in "${SOURCE_FILES[@]}"; do
        base=$(basename "${f%.mp4}")
        csv="$AUTODIR/csv/${base}-Scenes.csv"
        [ -f "$csv" ] || continue

        existing=$(ls "$AUTODIR/autocut/${base}-scene-"*.mp4 2>/dev/null | wc -l)
        expected=$(tail -n +3 "$csv" | wc -l)

        if [ "$existing" -ge "$expected" ] && [ "$existing" -gt 0 ]; then
            echo "  ✓ $base ($existing scenes, cached)"
            continue
        fi

        echo "  → $base ($expected scenes)"
        (
            scenedetect -i "$f" \
                load-scenes -i "$csv" \
                split-video -o "$AUTODIR/autocut/" \
                --filename "${base}-scene-\$SCENE_NUMBER" \
                --copy 2>/dev/null || true
        ) &
        PIDS+=($!)
    done
    for pid in "${PIDS[@]}"; do wait "$pid" || true; done
fi

SCENE_COUNT=$(ls "$AUTODIR/autocut/"*.mp4 2>/dev/null | wc -l)
echo "  Total: $SCENE_COUNT scenes"
if [ "$SCENE_COUNT" -eq 0 ]; then
    echo "ERROR: No scenes produced. Check source files and scenedetect output."
    exit 1
fi

# ── Frame Extraction ──────────────────────────────────────────────────────────
echo ""
echo "[4/6] Extracting key frames..."

MAX_JOBS=$(nproc)
_running=0
for f in "$AUTODIR/autocut/"*.mp4; do
    [ -f "$f" ] || continue
    outjpg="$AUTODIR/frames/$(basename "${f%.mp4}").jpg"
    [ -f "$outjpg" ] && continue
    size=$(stat -c%s "$f")
    [ "$size" -lt 5000000 ] && continue
    (
        duration=$($FFPROBE -v quiet -show_entries format=duration \
            -of csv=p=0 "$f" 2>/dev/null)
        [ -z "$duration" ] && exit 0
        mid=$(awk "BEGIN {printf \"%.3f\", $duration / 2}")
        $FFMPEG $HWACCEL -ss "$mid" -i "$f" \
            -vframes 1 -q:v 2 -update 1 \
            "$outjpg" -y -loglevel quiet
    ) &
    _running=$((_running + 1))
    if [ "$_running" -ge "$MAX_JOBS" ]; then
        wait -n 2>/dev/null || wait
        _running=$((_running - 1))
    fi
done
wait

FRAME_COUNT=$(ls "$AUTODIR/frames/"*.jpg 2>/dev/null | wc -l)
echo "  Frames: $FRAME_COUNT"
if [ "$FRAME_COUNT" -eq 0 ]; then
    echo "ERROR: No frames extracted. All scenes may be < 5MB or unreadable."
    exit 1
fi

# ── CLIP Scoring ──────────────────────────────────────────────────────────────
echo ""
echo "[5/6] CLIP scoring..."

if [ ! -f "$AUTODIR/scene_scores.csv" ]; then
    FRAMES_DIR="$AUTODIR/frames/" \
    OUTPUT_CSV="$AUTODIR/scene_scores.csv" \
        python3 "$SCRIPT_DIR"/clip_score.py
else
    echo "  Cached (delete $AUTODIR/scene_scores.csv to rescore)"
fi

if [ ! -f "$AUTODIR/scene_scores.csv" ]; then
    echo "ERROR: CLIP scoring failed, no scene_scores.csv produced."
    exit 1
fi

# ── Select & Concat ───────────────────────────────────────────────────────────
echo ""
echo "[6/6] Selecting scenes and building highlight..."

run_select_scenes() {
    SCENES_DIR="$AUTODIR/autocut/" \
    TRIMMED_DIR="$AUTODIR/trimmed/" \
    OUTPUT_CSV="$AUTODIR/scene_scores.csv" \
    OUTPUT_LIST="$AUTODIR/selected_scenes.txt" \
    CAM_SOURCES="$AUTODIR/camera_sources.csv" \
    AUDIO_CAM="$CAM_A" \
        python3 "$SCRIPT_DIR"/select_scenes.py "$THRESHOLD" "$MAX_SCENE" "$PER_FILE"
}

run_select_scenes

if [ "$THRESHOLD_EXPLICIT" -eq 0 ]; then
    while true; do
        if [ ! -s "$AUTODIR/selected_scenes.txt" ]; then
            echo ""
            echo "  No scenes selected at threshold $THRESHOLD."
        fi
        printf "  Threshold: %s  — Enter to continue, or new value to redo: " "$THRESHOLD"
        read -r USER_THRESHOLD </dev/tty
        USER_THRESHOLD="${USER_THRESHOLD%$'\r'}"
        [ -z "$USER_THRESHOLD" ] && break
        THRESHOLD="$USER_THRESHOLD"
        echo "  Re-running selection with threshold $THRESHOLD..."
        run_select_scenes
    done
fi

if [ ! -s "$AUTODIR/selected_scenes.txt" ]; then
    echo "ERROR: No scenes selected. Try lowering --threshold (current: $THRESHOLD)."
    exit 1
fi

HIGHLIGHT="$WORKDIR/highlight.mp4"

# Calculate total duration for progress bar
CONCAT_DUR=$(python3 -c "
import subprocess, re
total = 0
with open('$AUTODIR/selected_scenes.txt') as f:
    for line in f:
        m = re.match(r\"file '(.+)'\", line.strip())
        if m:
            r = subprocess.run(['$FFPROBE', '-v', 'quiet', '-show_entries', 'format=duration',
                                '-of', 'csv=p=0', m.group(1)], capture_output=True, text=True)
            try: total += float(r.stdout.strip())
            except: pass
print(f'{total:.1f}')
" 2>/dev/null || echo "0")

$FFMPEG -f concat -safe 0 \
    -i "$AUTODIR/selected_scenes.txt" \
    -vf "scale=${RESOLUTION}:flags=lanczos:force_original_aspect_ratio=decrease,pad=${RESOLUTION}:(ow-iw)/2:(oh-ih)/2:color=black" \
    -c:v "$VID_CODEC" $VID_QUALITY \
    -c:a aac -b:a "$AUDIO_BITRATE" \
    -pix_fmt yuv420p -r "$FRAMERATE" -vsync cfr \
    -progress pipe:1 -loglevel error \
    "$HIGHLIGHT" -y | \
python3 -c "
import sys, math
total = float('$CONCAT_DUR') or 1
for line in sys.stdin:
    k, _, v = line.strip().partition('=')
    if k == 'out_time_ms':
        try:
            cur = int(v) / 1_000_000
            pct = min(int(cur * 100 / total), 100)
            filled = pct // 2
            bar = '█' * filled + '░' * (50 - filled)
            print(f'\r  [{bar}] {pct:3d}%  {cur:.1f}/{total:.1f}s', end='', flush=True)
        except: pass
print()
" || true

HL_DURATION=$($FFPROBE -v quiet -show_entries format=duration \
    -of csv=p=0 "$HIGHLIGHT")

# ── Intro / Outro ─────────────────────────────────────────────────────────────
if [ "$NO_INTRO" -eq 0 ]; then
    echo ""
    echo "Adding intro/outro..."

    # Best frame (highest CLIP score)
    BEST_FRAME=$(python3 -c "
import pandas as pd
df = pd.read_csv('$AUTODIR/scene_scores.csv')
print('$AUTODIR/frames/' + df.iloc[0]['scene'] + '.jpg')
")

    WIDTH=$($FFPROBE -v quiet -show_entries stream=width \
        -of csv=p=0 "$HIGHLIGHT" | head -1)
    HEIGHT=$($FFPROBE -v quiet -show_entries stream=height \
        -of csv=p=0 "$HIGHLIGHT" | head -1)

    INTRO="$AUTODIR/intro.mp4"
    OUTRO="$AUTODIR/outro.mp4"
    HIGHLIGHT_FADED="$AUTODIR/highlight_faded.mp4"
    FINAL="$WORKDIR/highlight_final.mp4"

    # Split title into two lines for drawtext
    LINE1=$(echo -e "$TITLE" | head -1)
    LINE2=$(echo -e "$TITLE" | tail -n +2 | tr '\n' ' ' | xargs)

    FADE_OUT_ST=$(echo "$INTRO_DURATION - $FADE_DUR" | bc)

    # Intro
    $FFMPEG -loop 1 -i "$BEST_FRAME" \
        -f lavfi -i anullsrc=r=48000:cl=stereo \
        -t "$INTRO_DURATION" \
        -vf "scale=${WIDTH}:${HEIGHT}:force_original_aspect_ratio=decrease,
             pad=${WIDTH}:${HEIGHT}:(ow-iw)/2:(oh-ih)/2,
             drawtext=text='${LINE1}':fontfile=${FONT}:fontsize=${FONT_SIZE_TITLE}:
             fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-80:
             shadowcolor=black:shadowx=4:shadowy=4,
             drawtext=text='${LINE2}':fontfile=${FONT}:fontsize=${FONT_SIZE_SUBTITLE}:
             fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2+60:
             shadowcolor=black:shadowx=4:shadowy=4,
             fade=t=in:st=0:d=${FADE_DUR},fade=t=out:st=${FADE_OUT_ST}:d=${FADE_DUR}" \
        -map 0:v -map 1:a \
        -c:v "$VID_CODEC" $VID_QUALITY -pix_fmt yuv420p -r "$FRAMERATE" \
        -c:a aac -ar 48000 -ac 2 \
        "$INTRO" -y -loglevel quiet

    # Highlight with fade in/out
    FADE_OUT=$(echo "$HL_DURATION - $FADE_DUR" | bc)
    $FFMPEG -i "$HIGHLIGHT" \
        -vf "fade=t=in:st=0:d=${FADE_DUR},fade=t=out:st=${FADE_OUT}:d=${FADE_DUR}" \
        -c:v "$VID_CODEC" $VID_QUALITY -pix_fmt yuv420p -c:a copy \
        "$HIGHLIGHT_FADED" -y -loglevel quiet

    # Outro
    $FFMPEG -f lavfi \
        -i "color=c=black:s=${WIDTH}x${HEIGHT}:d=${INTRO_DURATION}:r=${FRAMERATE}" \
        -f lavfi -i anullsrc=r=48000:cl=stereo \
        -vf "drawtext=text='${OUTRO_TEXT}':
             fontfile=${FONT}:fontsize=${FONT_SIZE_OUTRO}:fontcolor=white:
             x=(w-text_w)/2:y=(h-text_h)/2:
             shadowcolor=gray:shadowx=2:shadowy=2,
             fade=t=in:st=0:d=${FADE_DUR},fade=t=out:st=${FADE_OUT_ST}:d=${FADE_DUR}" \
        -map 0:v -map 1:a \
        -c:v "$VID_CODEC" $VID_QUALITY -pix_fmt yuv420p -r "$FRAMERATE" \
        -c:a aac -ar 48000 -ac 2 -t "$INTRO_DURATION" \
        "$OUTRO" -y -loglevel quiet

    # Final concat
    cat > "$AUTODIR/final_concat.txt" << CONCATEOF
file '$INTRO'
file '$HIGHLIGHT_FADED'
file '$OUTRO'
CONCATEOF

    $FFMPEG -f concat -safe 0 \
        -i "$AUTODIR/final_concat.txt" \
        -c copy \
        "$FINAL" -y -loglevel quiet

    FINAL_DURATION=$($FFPROBE -v quiet -show_entries format=duration \
        -of csv=p=0 "$FINAL")
    FMIN=$(echo "$FINAL_DURATION" | awk '{printf "%d", $1/60}')
    FSEC=$(echo "$FINAL_DURATION" | awk '{printf "%d", $1%60}')

    echo "  highlight_final.mp4: ${FMIN}m${FSEC}s"
fi

# ── Music mix ─────────────────────────────────────────────────────────────────
if [ "$NO_MUSIC" -eq 0 ] && [ -d "$MUSIC_DIR" ]; then
    echo ""
    echo "Adding music..."

    MUSIC_INDEX="$MUSIC_DIR/index.json"

    # Build index if missing or new files added
    MP3_COUNT=$(ls "$MUSIC_DIR"/*.mp3 2>/dev/null | wc -l)
    INDEXED_COUNT=0
    [ -f "$MUSIC_INDEX" ] && INDEXED_COUNT=$(python3 -c "import json; print(len(json.load(open('$MUSIC_INDEX'))))" 2>/dev/null || echo 0)

    if [ ! -f "$MUSIC_INDEX" ] || [ "$MP3_COUNT" -gt "$INDEXED_COUNT" ]; then
        echo "  Building music index ($MP3_COUNT tracks)..."
        python3 "$SCRIPT_DIR"/music_index.py "$MUSIC_DIR" --output "$MUSIC_INDEX"
    else
        echo "  Music index: $INDEXED_COUNT tracks (cached)"
    fi

    # Pick track based on average CLIP score
    VIDEO_TO_MIX="${FINAL:-$HIGHLIGHT}"
    VIDEO_DURATION=$($FFPROBE -v quiet -show_entries format=duration \
        -of csv=p=0 "$VIDEO_TO_MIX")

    SELECTED_TRACK=$(python3 - <<PYEOF
import json, pandas as pd, sys

scores_csv = "$AUTODIR/scene_scores.csv"
index_path = "$MUSIC_INDEX"
duration   = float("$VIDEO_DURATION")

df = pd.read_csv(scores_csv)
avg_score = df["score"].mean()
# Map avg CLIP score to energy_norm target (0.148 low → 0.3, 0.18+ high → 0.9)
energy_target = min(0.9, max(0.2, (avg_score - 0.14) * 10))

import sys
all_tracks = json.load(open(index_path))

# Genre filter
genre_filter = "$MUSIC_GENRE".strip().lower()
if genre_filter:
    filtered = [t for t in all_tracks if genre_filter in t.get("genre", "").lower()]
    if filtered:
        all_tracks = filtered
        print(f"Genre filter '{genre_filter}': {len(all_tracks)} tracks", file=sys.stderr)
    else:
        print(f"No tracks for genre '{genre_filter}', using full library", file=sys.stderr)

# Artist filter (comma-separated)
artist_filter = "$MUSIC_ARTIST".strip().lower()
if artist_filter:
    artists = [a.strip() for a in artist_filter.split(",") if a.strip()]
    filtered = [t for t in all_tracks if any(a in t.get("title", "").lower() for a in artists)]
    if filtered:
        all_tracks = filtered
        print(f"Artist filter '{artist_filter}': {len(all_tracks)} tracks", file=sys.stderr)
    else:
        print(f"No tracks for artist '{artist_filter}', using previous selection", file=sys.stderr)

# Ideally: track longer than video (no cutoff), pick best energy among those
long_enough = [t for t in all_tracks if t["duration"] >= duration]
import random
if long_enough:
    # Prefer tracks close in length to the video; energy as tiebreak
    long_enough.sort(key=lambda t: (t["duration"] - duration, abs(t.get("energy_norm", 0.5) - energy_target)))
    best = random.choice(long_enough[:5])
else:
    all_tracks.sort(key=lambda t: (duration - t["duration"], abs(t.get("energy_norm", 0.5) - energy_target)))
    best = random.choice(all_tracks[:5])

print(f"video={duration:.0f}s  track={best['duration']:.0f}s  bpm={best['bpm']}  energy={best.get('energy_norm',0):.2f}  {best['title'][:50]}", file=sys.stderr)
print(best["file"])
PYEOF
)

    if [ -z "$SELECTED_TRACK" ]; then
        echo "  Could not select track, skipping music"
    else
        TRACK_NAME=$(basename "$SELECTED_TRACK" .mp3)
        echo "  Track: $TRACK_NAME"

        OUTPUT_MUSIC="${VIDEO_TO_MIX%.mp4}_music.mp4"

        FADE_START=$(echo "$VIDEO_DURATION - $MUSIC_FADE_DUR" | bc)

        $FFMPEG -i "$VIDEO_TO_MIX" -i "$SELECTED_TRACK" \
            -filter_complex "
                [0:a]volume=${ORIG_VOL}[orig];
                [1:a]atrim=0:${VIDEO_DURATION},afade=t=out:st=${FADE_START}:d=${MUSIC_FADE_DUR},volume=${MUSIC_VOL}[music];
                [orig][music]amix=inputs=2:duration=first[aout]
            " \
            -map 0:v -map "[aout]" \
            -c:v copy -c:a aac -b:a 192k \
            "$OUTPUT_MUSIC" -y -loglevel quiet

        echo "  → $(basename "$OUTPUT_MUSIC")"
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
ELAPSED=$(( SECONDS - PIPELINE_START ))
ELAPSED_MIN=$(( ELAPSED / 60 ))
ELAPSED_SEC=$(( ELAPSED % 60 ))
HL_MIN=$(echo "$HL_DURATION" | awk '{printf "%d", $1/60}')
HL_SEC=$(echo "$HL_DURATION" | awk '{printf "%d", $1%60}')
SCENE_COUNT=$(wc -l < "$AUTODIR/selected_scenes.txt")

echo ""
echo "╔══════════════════════════════════════╗"
echo "║              DONE                    ║"
echo "╠══════════════════════════════════════╣"
printf "║ %-36s ║\n" "highlight.mp4:       ${HL_MIN}m${HL_SEC}s"
[ "$NO_INTRO" -eq 0 ] && printf "║ %-36s ║\n" "highlight_final.mp4: ${FMIN}m${FSEC}s"
printf "║ %-36s ║\n" "Scenes: $SCENE_COUNT"
printf "║ %-36s ║\n" "Total time: ${ELAPSED_MIN}m${ELAPSED_SEC}s"
echo "╚══════════════════════════════════════╝"
