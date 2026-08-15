"""
Road-width measurement from satellite imagery
==============================================
Measure how far the road (asphalt) extends on each side of a marked point,
perpendicular to the road direction, so a marking polygon can span the road
end-to-end. Uses Esri World Imagery tiles (no API key), cached on disk.

measure(lat, lon, bearing) -> (left_m, right_m, confidence) or None
  left_m / right_m : metres from the point to the road edge on each side
                     ("left" = 90 deg CCW of the travel direction).
  confidence       : 0..1, agreement across sampling lines.
"""
import math, os, urllib.request
import numpy as np
from PIL import Image

ZOOM      = 19
TILE      = 256
HALF_M    = 55.0                      # half patch size (metres)
STEP      = 0.25                      # march step (metres)
BRIDGE_M  = 1.5                       # bridge non-road gaps up to this (markings, thin shadow)
TOL_V     = 38                        # brightness tolerance vs the road sample
TOL_S     = 42                        # saturation tolerance vs the road sample
PROFILES  = (-3.0, -1.5, 0.0, 1.5, 3.0)   # sampling lines offset along the road (m)
MIN_HALF  = 1.5                       # clamp each side to >= this (m)
MAX_HALF  = 12.0                      # and <= this (m)

CACHE_DIR = os.path.join("cache", "tiles")
TILE_URL  = ("https://services.arcgisonline.com/ArcGIS/rest/services/"
             "World_Imagery/MapServer/tile/{z}/{y}/{x}")


def _global_px(lat, lon):
    n = 2 ** ZOOM * TILE
    x = (lon + 180.0) / 360.0 * n
    lr = math.radians(lat)
    y = (1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * n
    return x, y


def _tile(X, Y):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{ZOOM}_{X}_{Y}.png")
    if not os.path.exists(path):
        url = TILE_URL.format(z=ZOOM, x=X, y=Y)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        with open(path, "wb") as f:
            f.write(data)
    return Image.open(path).convert("RGB")


def fetch_patch(lat, lon):
    """Return (hsv_array, mpp, cx, cy) for a patch centred on (lat,lon), or None."""
    mpp = 156543.03392 * math.cos(math.radians(lat)) / 2 ** ZOOM
    gx, gy = _global_px(lat, lon)
    half = HALF_M / mpp
    x0, y0, x1, y1 = gx - half, gy - half, gx + half, gy + half
    tX0, tY0, tX1, tY1 = int(x0 // TILE), int(y0 // TILE), int(x1 // TILE), int(y1 // TILE)
    canvas = Image.new("RGB", ((tX1 - tX0 + 1) * TILE, (tY1 - tY0 + 1) * TILE))
    try:
        for X in range(tX0, tX1 + 1):
            for Y in range(tY0, tY1 + 1):
                canvas.paste(_tile(X, Y), ((X - tX0) * TILE, (Y - tY0) * TILE))
    except Exception:
        return None
    ox, oy = tX0 * TILE, tY0 * TILE
    crop = canvas.crop((int(x0 - ox), int(y0 - oy), int(x1 - ox), int(y1 - oy)))
    hsv = np.asarray(crop.convert("HSV")).astype(np.float32)
    return hsv, mpp, gx - x0, gy - y0


def measure(lat, lon, bearing_deg, cap_left=float("inf"), cap_right=float("inf")):
    patch = fetch_patch(lat, lon)
    if patch is None:
        return None
    hsv, mpp, cx, cy = patch
    H, W = hsv.shape[:2]

    br = math.radians(bearing_deg)
    a_e, a_n = math.sin(br), math.cos(br)            # along road (E,N)
    nl_e, nl_n = -math.cos(br), math.sin(br)         # left normal (90 deg CCW)

    # road colour sampled at the clicked point
    b = 4
    box = hsv[max(0, int(cy)-b):int(cy)+b+1, max(0, int(cx)-b):int(cx)+b+1].reshape(-1, 3)
    roadS = float(np.median(box[:, 1]))
    roadV = float(np.median(box[:, 2]))

    def is_road(px, py):
        xi, yi = int(round(px)), int(round(py))
        if not (0 <= xi < W and 0 <= yi < H):
            return False
        h, s, v = hsv[yi, xi]
        if 45 < h < 110 and s > 55:          # saturated green -> vegetation
            return False
        if v < 20 or v > 238:                # deep shadow / blown-out highlight
            return False
        return abs(v - roadV) < TOL_V and s < roadS + TOL_S

    def march(ox, oy, sign, cap):
        # sign=+1 -> left normal, -1 -> right normal
        e, nn = sign * nl_e, sign * nl_n
        gap, edge, d = 0.0, 0.0, 0.0
        limit = min(HALF_M - 1, cap)
        while d < limit:
            d += STEP
            px = ox + (e * d) / mpp
            py = oy - (nn * d) / mpp
            if is_road(px, py):
                gap = 0.0; edge = d
            else:
                gap += STEP
                if gap > BRIDGE_M:
                    break
        return edge

    lefts, rights = [], []
    for da in PROFILES:
        ox = cx + (a_e * da) / mpp
        oy = cy - (a_n * da) / mpp
        lefts.append(march(ox, oy, +1, cap_left))
        rights.append(march(ox, oy, -1, cap_right))

    Lm = float(np.median(lefts))
    Rm = float(np.median(rights))
    spread = float(np.std(lefts) + np.std(rights))
    total = Lm + Rm
    conf = 1.0 - min(1.0, spread / max(total, 1.0))

    Lm = min(max(Lm, MIN_HALF), MAX_HALF)
    Rm = min(max(Rm, MIN_HALF), MAX_HALF)
    return Lm, Rm, conf
