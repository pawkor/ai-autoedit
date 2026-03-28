# Kamera 360° i reframe / 360° camera and reframe

## PL

### Jak to działa

Pipeline obsługuje Insta360 X2 przez pliki `.insv`. Kamera generuje dwa typy:

- **LRV** (`LRV_TIMESTAMP_11_NNN.insv`) — low-res proxy, ok. 736×368, equirectangular, używany do detekcji scen i scoringu CLIP
- **VID_** (`VID_TIMESTAMP_10_NNN.insv`) — high-res, 2880×2880, dual fisheye, używany do finalnego renderu (opcjonalnie)

Gdy `--cam-b` wskazuje na katalog z plikami `LRV_*.insv`, reframe uruchamia się automatycznie.

### Krok 0 — reframe LRV (proxy do detekcji)

Pliki LRV są reprojekcjonowane z equirectangular → rectilinear przez `v360` ffmpeg. Wynik trafia do `_autoframe/reframed/` i jest traktowany jak zwykły MP4 kamery B.

### Krok 6.5 — proxy reframe VID_ (opcjonalnie, do finalnego renderu)

Jeśli `vid_input_format` jest ustawiony w `[reframe]`, pipeline po selekcji scen zastępuje wybrane klipy LRV wersjami VID_ high-res:

- Mapowanie pliku: `LRV_TIMESTAMP_11_NNN` → `VID_TIMESTAMP_10_NNN.insv`
- Czas wycięcia wyznaczany z CSV detekcji scen — ten sam środek co LRV
- Format `v360=dfisheye:rectilinear` dla dual fisheye VID_
- Wynik w `_autoframe/vid_trimmed/`

Bez `vid_input_format` w config.ini krok 6.5 jest pomijany — w finalnym filmie zostają klipy LRV.

### Konfiguracja `[reframe]`

| Klucz | Domyślnie | Opis |
|-------|-----------|------|
| `yaw` | `0` | Obrót poziomy. `0`=przód, `90`=bok, `180`=tył. |
| `pitch` | `0` | Pochylenie. `0`=poziom, ujemne=w dół. Zakres: `-180` do `180`. |
| `roll` | `0` | Rotacja obrazu. `90`/`-90` do korekcji przekrzywionej kamery. |
| `h_fov` | `100` | Poziome pole widzenia w stopniach. |
| `v_fov` | `75` | Pionowe pole widzenia w stopniach. |
| `vid_input_format` | *(brak)* | Format wejściowy VID_. Ustaw `dfisheye` dla Insta360 X2. Bez tego klucza krok 6.5 jest pomijany. |
| `vid_ih_fov` | `190` | Pole widzenia wejściowe dla dual fisheye VID_. |

### Kalibracja kąta

Jednorazowo dla nowego montażu kamery:

```bash
for yaw in 0 90 180 270; do
  for pitch in -90 -45 0 45 90; do
    ffmpeg -i LRV_*.insv \
      -vf "v360=equirect:rectilinear:yaw=${yaw}:pitch=${pitch}:h_fov=100:v_fov=75" \
      -frames:v 1 "test_y${yaw}_p${pitch}.jpg" -y -loglevel quiet 2>/dev/null
  done
done
```

Znajdź klatkę z właściwym widokiem. Dodaj korekcję `roll` jeśli obraz jest przekrzywiony. Następnie dostosuj `pitch` w małych krokach (np. `±5°`) żeby ustawić właściwy kąt pionowy.

> **Uwaga:** Zakres pitch to `-180` do `180`. Wartości takie jak `185` spowodują błąd ffmpeg.

### Przykład — kamera na lusterku, stick 1m w górę, widok do tyłu

```ini
[reframe]
yaw   = 90
pitch = 160
roll  = 90
h_fov = 100
v_fov = 70
vid_input_format = dfisheye
vid_ih_fov       = 190
```

---

## EN

### How it works

The pipeline handles Insta360 X2 through `.insv` files. The camera generates two types:

- **LRV** (`LRV_TIMESTAMP_11_NNN.insv`) — low-res proxy, ~736×368, equirectangular, used for scene detection and CLIP scoring
- **VID_** (`VID_TIMESTAMP_10_NNN.insv`) — high-res, 2880×2880, dual fisheye, used for final render (optional)

Reframe runs automatically when `--cam-b` points to a directory containing `LRV_*.insv` files.

### Step 0 — LRV reframe (proxy for detection)

LRV files are reprojected equirectangular → rectilinear via ffmpeg `v360`. Output lands in `_autoframe/reframed/` and is treated as regular camera B footage.

### Step 6.5 — VID_ proxy reframe (optional, for final render)

When `vid_input_format` is set in `[reframe]`, the pipeline replaces selected LRV clips with high-res VID_ versions after scene selection:

- File mapping: `LRV_TIMESTAMP_11_NNN` → `VID_TIMESTAMP_10_NNN.insv`
- Timing derived from scene detection CSV — same midpoint as LRV clip
- Format `v360=dfisheye:rectilinear` for dual fisheye VID_
- Output in `_autoframe/vid_trimmed/`

Without `vid_input_format` in config.ini, step 6.5 is skipped — LRV clips remain in the final video.

### `[reframe]` configuration

| Key | Default | Description |
|-----|---------|-------------|
| `yaw` | `0` | Horizontal rotation. `0`=front, `90`=side, `180`=rear. |
| `pitch` | `0` | Vertical tilt. `0`=level, negative=down. Range: `-180` to `180`. |
| `roll` | `0` | Image rotation. `90`/`-90` to correct a tilted camera. |
| `h_fov` | `100` | Horizontal field of view in degrees. |
| `v_fov` | `75` | Vertical field of view in degrees. |
| `vid_input_format` | *(unset)* | Input format for VID_ files. Set to `dfisheye` for Insta360 X2. Without this key, step 6.5 is skipped. |
| `vid_ih_fov` | `190` | Input field of view for dual fisheye VID_. |

### Angle calibration

One-time process per camera mount:

```bash
for yaw in 0 90 180 270; do
  for pitch in -90 -45 0 45 90; do
    ffmpeg -i LRV_*.insv \
      -vf "v360=equirect:rectilinear:yaw=${yaw}:pitch=${pitch}:h_fov=100:v_fov=75" \
      -frames:v 1 "test_y${yaw}_p${pitch}.jpg" -y -loglevel quiet 2>/dev/null
  done
done
```

Find the frame with the correct view. Add `roll` correction if the image is tilted. Fine-tune `pitch` in small steps (e.g. `±5°`) to set the correct vertical angle.

> **Note:** pitch range is `-180` to `180`. Values like `185` will cause an ffmpeg error.

### Example — mirror mount, 1m vertical stick, rear-facing view

```ini
[reframe]
yaw   = 90
pitch = 160
roll  = 90
h_fov = 100
v_fov = 70
vid_input_format = dfisheye
vid_ih_fov       = 190
```
