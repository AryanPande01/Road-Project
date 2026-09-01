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
import math, os, threading, requests
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

# CACHE_DIR = os.path.join("cache", "tiles")
TILE_URL  = ("https://services.arcgisonline.com/ArcGIS/rest/services/"
             "World_Imagery/MapServer/tile/{z}/{y}/{x}")

_SESSION = None
_SESSION_LOCK = threading.Lock()

# ── Tile cache ───────────────────────────────────────────────
# Zoom-19 satellite tiles are ~30 m across, so every placemark on the same road
# corridor re-requests the SAME handful of tiles. Fetching them fresh for each
# of 100-150 points was the dominant cost in KML generation. We cache decoded
# tiles in memory and de-duplicate concurrent requests for the same tile so the
# many worker threads never fetch one tile twice. This is the single biggest
# latency win and is safe: satellite imagery for a fixed (z,x,y) is immutable.
_TILE_CACHE = {}                 # (X, Y) -> PIL.Image | None (None = known-miss)
_TILE_CACHE_LOCK = threading.Lock()
_TILE_EVENTS = {}                # (X, Y) -> threading.Event for in-flight fetches
_TILE_CACHE_MAX = 4096           # generous bound; a huge job spans few tiles


def clear_tile_cache():
    """Drop the in-memory tile cache (used by benchmarks / long-running servers)."""
    with _TILE_CACHE_LOCK:
        _TILE_CACHE.clear()
        _TILE_EVENTS.clear()


def get_session():
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                s = requests.Session()
                s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
                adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=100, max_retries=2)
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _SESSION = s
    return _SESSION


def _global_px(lat, lon):
    n = 2 ** ZOOM * TILE
    x = (lon + 180.0) / 360.0 * n
    lr = math.radians(lat)
    y = (1 - math.log(math.tan(lr) + 1 / math.cos(lr)) / math.pi) / 2 * n
    return x, y


def _download_tile(X, Y):
    """Fetch and decode a single tile from the network. Returns a PIL image or
    None on any failure."""
    try:
        url = TILE_URL.format(z=ZOOM, x=X, y=Y)
        s = get_session()
        r = s.get(url, timeout=12)
        if r.status_code == 200 and len(r.content) > 100:
            import io
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        pass
    return None


def _tile(X, Y):
    """Return the decoded tile at (X, Y), from cache when possible.

    Thread-safe with single-flight semantics: if one worker is already fetching
    a tile, other workers wait on its result instead of issuing a duplicate
    request. Misses are cached as None so a genuinely missing tile isn't retried
    for every point that needs it.
    """
    key = (X, Y)
    with _TILE_CACHE_LOCK:
        if key in _TILE_CACHE:
            return _TILE_CACHE[key]
        event = _TILE_EVENTS.get(key)
        if event is None:
            # We are the first to want this tile -> we fetch it.
            event = threading.Event()
            _TILE_EVENTS[key] = event
            owner = True
        else:
            owner = False

    if not owner:
        # Another thread is fetching this tile; wait for it, then read the cache.
        event.wait(timeout=15)
        with _TILE_CACHE_LOCK:
            return _TILE_CACHE.get(key)

    # We own the fetch.
    img = _download_tile(X, Y)
    with _TILE_CACHE_LOCK:
        if len(_TILE_CACHE) < _TILE_CACHE_MAX:
            _TILE_CACHE[key] = img
        _TILE_EVENTS.pop(key, None)
    event.set()
    return img


def fetch_patch(lat, lon):
    """Return (hsv_array, mpp, cx, cy) for a patch centred on (lat,lon), or None."""
    from concurrent.futures import ThreadPoolExecutor
    mpp = 156543.03392 * math.cos(math.radians(lat)) / 2 ** ZOOM
    gx, gy = _global_px(lat, lon)
    half = HALF_M / mpp
    x0, y0, x1, y1 = gx - half, gy - half, gx + half, gy + half
    tX0, tY0, tX1, tY1 = int(x0 // TILE), int(y0 // TILE), int(x1 // TILE), int(y1 // TILE)
    
    tiles_to_fetch = [(X, Y) for X in range(tX0, tX1 + 1) for Y in range(tY0, tY1 + 1)]
    tile_images = {}
    
    def fetch_single_tile(coords):
        X, Y = coords
        try:
            return coords, _tile(X, Y)
        except Exception:
            return coords, None

    with ThreadPoolExecutor(max_workers=min(8, len(tiles_to_fetch))) as executor:
        for coords, tile_img in executor.map(fetch_single_tile, tiles_to_fetch):
            if tile_img is not None:
                tile_images[coords] = tile_img

    canvas = Image.new("RGB", ((tX1 - tX0 + 1) * TILE, (tY1 - tY0 + 1) * TILE))
    try:
        for X in range(tX0, tX1 + 1):
            for Y in range(tY0, tY1 + 1):
                if (X, Y) in tile_images:
                    canvas.paste(tile_images[(X, Y)], ((X - tX0) * TILE, (Y - tY0) * TILE))
    except Exception:
        return None
    ox, oy = tX0 * TILE, tY0 * TILE
    crop = canvas.crop((int(x0 - ox), int(y0 - oy), int(x1 - ox), int(y1 - oy)))
    hsv = np.asarray(crop.convert("HSV")).astype(np.float32)
    return hsv, mpp, gx - x0, gy - y0


def measure(lat, lon, bearing_deg, cap_left=float("inf"), cap_right=float("inf")):
    """Measure road width using Sobel edge detection on the brightness profile
    perpendicular to the road.  Returns (left_m, right_m, confidence)."""
    patch = fetch_patch(lat, lon)
    if patch is None:
        return None
    hsv, mpp, cx, cy = patch
    H, W = hsv.shape[:2]

    br = math.radians(bearing_deg)
    a_e, a_n = math.sin(br), math.cos(br)            # along road (E,N)
    nl_e, nl_n = -math.cos(br), math.sin(br)         # left normal (90 deg CCW)

    def _profile(ox, oy, sign, max_dist):
        """Sample brightness (V channel) along the normal direction."""
        e, nn = sign * nl_e, sign * nl_n
        vals = []
        d = 0.0
        while d <= max_dist:
            px = ox + (e * d) / mpp
            py = oy - (nn * d) / mpp
            xi, yi = int(round(px)), int(round(py))
            if not (0 <= xi < W and 0 <= yi < H):
                break
            h_val, s_val, v_val = hsv[yi, xi]
            vals.append((d, float(h_val), float(s_val), float(v_val)))
            d += STEP
        return vals

    def _find_edge(profile, cap):
        """Find the road edge in a brightness/saturation profile using gradient
        analysis.  Returns distance in metres to the edge."""
        if len(profile) < 6:
            return MIN_HALF

        dists = np.array([p[0] for p in profile])
        hues  = np.array([p[1] for p in profile])
        sats  = np.array([p[2] for p in profile])
        vals  = np.array([p[3] for p in profile])

        n = len(vals)

        # 1. Compute gradient magnitude of V and S channels (central diff)
        grad_v = np.zeros(n)
        grad_s = np.zeros(n)
        for i in range(1, n - 1):
            grad_v[i] = abs(float(vals[i+1]) - float(vals[i-1])) / 2.0
            grad_s[i] = abs(float(sats[i+1]) - float(sats[i-1])) / 2.0

        # Combined gradient (brightness changes + saturation changes at edge)
        grad_combined = grad_v + 0.7 * grad_s

        # 2. Look for strong edges (peaks in gradient) past a minimum distance
        min_search = 1.2  # don't detect edges within 1.2m of center (center lane marking etc)
        best_edge = min(cap, dists[-1])
        best_score = 0.0

        for i in range(2, n - 2):
            d = dists[i]
            if d < min_search or d > cap:
                continue

            score = grad_combined[i]

            # Boost score if there's a vegetation signature right after
            if i + 2 < n:
                future_h = np.mean(hues[i:min(i+4, n)])
                future_s = np.mean(sats[i:min(i+4, n)])
                if 35 < future_h < 135 and future_s > 35:
                    score *= 2.0  # vegetation boundary
                if future_s > 55:
                    score *= 1.5  # general high-saturation boundary (dirt/soil)

            # Boost if brightness drops significantly
            center_v = np.mean(vals[:max(3, int(min_search / STEP))])
            if i + 2 < n:
                future_v = np.mean(vals[i:min(i+4, n)])
                if abs(future_v - center_v) > 30:
                    score *= 1.3

            if score > best_score and score > 8.0:  # threshold for a real edge
                best_score = score
                best_edge = d

        # 3. Fallback: check for vegetation/soil directly
        for i in range(2, n):
            d = dists[i]
            if d < min_search or d > cap:
                continue
            h, s, v = hues[i], sats[i], vals[i]
            # Clear vegetation
            if 35 < h < 135 and s > 42:
                return max(MIN_HALF, d - STEP)
            # Reddish/brown dirt or soil
            if (h < 25 or h > 155) and s > 62 and v < 155:
                return max(MIN_HALF, d - STEP)

        return max(MIN_HALF, min(best_edge, cap))

    # Sample multiple profiles along the road and take the median
    left_cap = min(cap_left, HALF_M - 1)
    right_cap = min(cap_right, HALF_M - 1)

    lefts, rights = [], []
    for da in PROFILES:
        ox = cx + (a_e * da) / mpp
        oy = cy - (a_n * da) / mpp

        lp = _profile(ox, oy, +1, left_cap)
        rp = _profile(ox, oy, -1, right_cap)

        lefts.append(_find_edge(lp, left_cap))
        rights.append(_find_edge(rp, right_cap))

    Lm = float(np.median(lefts))
    Rm = float(np.median(rights))
    spread = float(np.std(lefts) + np.std(rights))
    total = Lm + Rm
    conf = 1.0 - min(1.0, spread / max(total, 1.0))

    Lm = min(max(Lm, MIN_HALF), MAX_HALF)
    Rm = min(max(Rm, MIN_HALF), MAX_HALF)
    return Lm, Rm, conf

