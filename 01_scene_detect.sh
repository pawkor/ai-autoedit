#!/bin/bash
set -uo pipefail

WORKDIR="$(pwd)"
SINGLE_FILE=""
THRESHOLD="20"
MIN_SCENE="15s"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -f) SINGLE_FILE="$2"; shift 2 ;;
        -t) THRESHOLD="$2"; shift 2 ;;
        -m) MIN_SCENE="$2"; shift 2 ;;
        *) shift ;;
    esac
done

echo "=== Scene Detection ==="
echo "Dir:       $WORKDIR"
echo "Threshold: $THRESHOLD"
echo "Min scene: $MIN_SCENE"
[[ -n "$SINGLE_FILE" ]] && echo "File:      $SINGLE_FILE"

mkdir -p autocut frames csv

process_file() {
    local f="$1"
    local base=$(basename "${f%.mp4}")
    echo "→ $base"

    local csv="$WORKDIR/csv/${base}-Scenes.csv"

    # Detekcja tylko jeśli brak CSV
    if [ ! -f "$csv" ]; then
        scenedetect -f 3 -i "$f" \
            detect-content --threshold "$THRESHOLD" --min-scene-len "$MIN_SCENE" \
            list-scenes -o "$WORKDIR/csv/" \
            2>/dev/null
    else
        echo "  CSV już istnieje, pomijam detekcję"
    fi

    [ -f "$csv" ] || { echo "  Brak CSV"; return; }

    # Wytnij sceny sekwencyjnie — kolumny: 1=scene_num, 4=start_sec, 10=len_sec
    local count=0
    while IFS=, read -r scene_num start_frame start_tc start_sec \
                         end_frame end_tc end_sec len_frames len_tc len_sec; do
        scene_num=$(echo "$scene_num" | tr -d ' \r\n')
        [[ "$scene_num" =~ ^[0-9]+$ ]] || continue

        local outfile="autocut/${base}-scene-$(printf '%03d' "$scene_num").mp4"
        [ -f "$outfile" ] && continue  # skip jeśli już istnieje

        ffmpeg -ss "$start_sec" -i "$f" -t "$len_sec" \
            -c copy "$outfile" -y -loglevel quiet
        count=$((count + 1))
    done < <(tail -n +3 "$csv")

    echo "  Scen: $(ls autocut/${base}-scene-*.mp4 2>/dev/null | wc -l)"
}

if [[ -n "$SINGLE_FILE" ]]; then
    process_file "$WORKDIR/$SINGLE_FILE"
else
    for f in "$WORKDIR"/*.mp4; do
        [ -f "$f" ] || continue
        process_file "$f"
    done
fi

find autocut/ -type f -size -500k -delete -print 2>/dev/null || true
echo ""
echo "Scen total: $(ls autocut/*.mp4 2>/dev/null | wc -l)"
