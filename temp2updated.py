"""
Road Marking Polygon Generator - v4 (curve-aware, road-fitting)
================================================================
Given a KML of marked points (exported from Google Earth), draw for each point
a polygon that:
  * spans the FULL WIDTH of the road it sits on (kerb to kerb of one carriageway),
  * follows the road's SHAPE / curvature (not a flat rectangle),
  * is aligned with the road's direction and alignment.

The polygons + a BOQ (bill of quantities) spreadsheet are written out so the
result can be opened in Google Earth and used for paint-cost estimation .

How it works
------------
1. Parse the input KML -> list of (lon, lat, name) marking points.
2. Query OpenStreetMap (Overpass API) ONCE for every road in the bounding box
   of all points, with on-disk caching and mirror fallback.
3. For each point:
     - snap it to the nearest suitable road centreline,
     - read the local road DIRECTION straight from the road geometry,
     - estimate the carriageway WIDTH (width tag -> lanes -> type default,
       with manual overrides),
     - build a curve-following ribbon polygon of the given length ALONG the
       road and the full width ACROSS it.
4. Write out.kml (polygons) and a BOQ .xlsx (area x rate = amount), recording
   for every point WHERE the width came from so estimated vs. known is visible.

A note on accuracy
------------------
Road *shape/direction* comes directly from the map and is accurate.
Road *width* is very often NOT present in the map data (as here: no `width`
tags anywhere in the sample area). It is therefore ESTIMATED from lane count
or road class. Where you know the true width, override it (see WIDTH section)
so the cost estimate is exact - the BOQ's "Width source" column tells you which
rows are estimates.

Dependencies: pandas, openpyxl, numpy   (no `requests` needed - uses stdlib)
Install: pip install pandas openpyxl numpy
Usage  : python temp2.py input.kml [more.kml ...]
         python temp2.py input.kml --length 4 --offline
"""

import math, os, re, json, time, hashlib, argparse, zipfile
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET
import numpy as np
import pandas as pd
import requests, io
import openpyxl
from openpyxl.drawing.image import Image as XLImage
from PIL import Image as PILImage

import config   # central placeholder -> item -> image -> cost mapping

# -- CONFIG --------------------------------------------------------
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]
MARK_LENGTH   = 4.0      # metres ALONG the road (depth of the marking strip)
RATE_PER_SQM  = 2500     # Rs/m2 for the BOQ
LANE_WIDTH    = 3.5      # assumed metres per traffic lane when estimating width
MEASURE_WIDTH = True     # measure each road's real width from satellite imagery
                         # (vegetation-aware); falls back to OSM class width when
                         # the reading is low-confidence or implausible
WIDTH_MIN_CONF = 0.55    # below this detection confidence, fall back to OSM width
OUTPUT_DIR    = "outputs"
CACHE_DIR     = "cache"
BBOX_MARGIN_M = 60       # extra margin around the points' bounding box (metres)
MAX_BBOX_M    = 4000     # if points span more than this, query per point instead
PER_POINT_RADIUS_M = 60  # half-size of the per-point query box (metres)
FAR_ROAD_WARN = 25.0     # warn if the nearest road is farther than this (metres)
SELF_ISECT_FALLBACK = True   # fall back to a straight strip if the ribbon self-intersects

# Road classes we are willing to snap to. Footways/paths are only used as a
# last resort (unlikely to be the intended carriageway).
DRIVEABLE = ["motorway", "trunk", "primary", "secondary", "tertiary",
             "unclassified", "residential", "service", "living_street",
             "motorway_link", "trunk_link", "primary_link", "secondary_link",
             "tertiary_link", "road"]
LAST_RESORT = ["footway", "path", "cycleway", "pedestrian", "track", "steps"]

# Snapping importance (lower = more major). Link/ramp roads inherit their parent
# class's importance so a trunk ramp isn't out-ranked by a tiny unclassified
# lane that happens to sit a metre closer to the point's centreline.
ROAD_IMPORTANCE = {
    "motorway": 0, "motorway_link": 0,
    "trunk": 1, "trunk_link": 1,
    "primary": 2, "primary_link": 2,
    "secondary": 3, "secondary_link": 3,
    "tertiary": 4, "tertiary_link": 4,
    "unclassified": 5, "road": 5,
    "residential": 6, "living_street": 7, "service": 8,
}

# Fallback carriageway width (metres) by road class, used only when neither a
# width tag nor a lane count is available. Tune to your region.
ROAD_WIDTH_DEFAULTS = {
    "motorway": 14.0, "trunk": 10.5, "primary": 9.0, "secondary": 8.0,
    "tertiary": 7.0,  "unclassified": 6.0, "residential": 5.5,
    "service": 4.0,   "living_street": 4.0,
    "motorway_link": 7.0, "trunk_link": 7.0, "primary_link": 6.5,
    "secondary_link": 6.0, "tertiary_link": 5.5, "road": 7.0,
    "footway": 2.0, "path": 1.5, "cycleway": 2.0, "pedestrian": 4.0,
    "track": 3.0, "steps": 1.5,
    "_default": 7.0,
}

# Shoulder / edge-strip allowance (metres, both sides combined) added to a
# lane-count width, so a marked carriageway includes its shoulders. Major roads
# have wider shoulders; minor roads effectively none.
SHOULDER_TOTAL = {
    "motorway": 5.0, "motorway_link": 3.0,
    "trunk": 4.0, "trunk_link": 3.0,
    "primary": 3.0, "primary_link": 2.5,
    "secondary": 2.5, "secondary_link": 2.0,
    "tertiary": 2.0, "tertiary_link": 1.5,
}

# Manual width overrides you are sure about. Keyed by placemark name OR by the
# 1-based index in the file as a string, e.g.  {"Gate 3": 12.0, "5": 10.5}.
# Also, a width embedded in a placemark name like "Stop line w=12" or "12m" is
# picked up automatically. Overrides always win and are marked "manual" in BOQ.
MANUAL_WIDTHS = {}

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
SCREENSHOTS_DIR = os.path.join(OUTPUT_DIR, "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)


# ── SATELLITE IMAGE WITH POLYGON OVERLAY ─────────────────────

def get_satellite_image(corners):
    """
    Fetch Esri satellite image at minimum viable bbox (0.001 deg),
    draw the polygon, then crop tightly around it with small padding.
    """
    lons = [c[0] for c in corners]
    lats = [c[1] for c in corners]
    cx = (min(lons) + max(lons)) / 2
    cy = (min(lats) + max(lats)) / 2
    d  = 0.001  # minimum Esri accepts (~110m)

    min_lon, max_lon = cx - d, cx + d
    min_lat, max_lat = cy - d, cy + d

    url = (
        "https://services.arcgisonline.com/arcgis/rest/services/"
        "World_Imagery/MapServer/export"
        "?bbox=" + str(min_lon) + "," + str(min_lat) + "," + str(max_lon) + "," + str(max_lat) +
        "&bboxSR=4326&size=700,700&imageSR=4326&format=png&f=image"
    )

    img = None
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200 and len(r.content) > 1000:
                img = PILImage.open(io.BytesIO(r.content)).convert("RGBA")
                break
        except Exception as e:
            print(" [retry " + str(attempt+1) + "] " + str(e))
            time.sleep(2)

    if img is None:
        return None

    W, H = img.size

    def to_px(lon, lat):
        x = (lon - min_lon) / (max_lon - min_lon) * W
        y = (max_lat - lat) / (max_lat - min_lat) * H
        return (max(0, min(W-1, int(x))), max(0, min(H-1, int(y))))

    pixel_pts = [to_px(lon, lat) for lon, lat in corners]
    if len(pixel_pts) > 1 and pixel_pts[0] == pixel_pts[-1]:
        pixel_pts = pixel_pts[:-1]

    # Draw polygon overlay
    from PIL import ImageDraw
    overlay = PILImage.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.polygon(pixel_pts, fill=(255, 255, 0, 120))
    # Draw outline using offset 1px polygon outlines to avoid the Pillow ImageDraw.line thick line shift bug
    for dx in [-1, 0, 1]:
        for dy in [-1, 0, 1]:
            draw.polygon([(x + dx, y + dy) for x, y in pixel_pts],
                         outline=(255, 50, 50, 255))
    img = PILImage.alpha_composite(img, overlay)

    # Crop tightly around the polygon with padding of ~30px
    px_xs = [p[0] for p in pixel_pts]
    px_ys = [p[1] for p in pixel_pts]
    pad = 40
    crop_box = (
        max(0,   min(px_xs) - pad),
        max(0,   min(px_ys) - pad),
        min(W,   max(px_xs) + pad),
        min(H,   max(px_ys) + pad),
    )
    img = img.crop(crop_box).convert("RGB")
    # Upscale so it's visible in the Excel cell
    img = img.resize((400, 400), PILImage.LANCZOS)
    return img


# ==================================================================
# Local metric projection  (equirectangular around a reference point)
# ==================================================================
# Working in metres lets us do clean vector geometry. Over the tens-of-metres
# scale of a road marking the distortion is utterly negligible.

class LocalFrame:
    def __init__(self, lat0, lon0):
        self.lat0, self.lon0 = lat0, lon0
        self.mx = 111_320.0 * math.cos(math.radians(lat0))  # metres per deg lon
        self.my = 111_320.0                                 # metres per deg lat

    def to_xy(self, lon, lat):
        return ((lon - self.lon0) * self.mx, (lat - self.lat0) * self.my)

    def to_lonlat(self, x, y):
        return (self.lon0 + x / self.mx, self.lat0 + y / self.my)


# ==================================================================
# Overpass fetch  (stdlib, cached, mirror fallback)
# ==================================================================

def _cache_path(key):
    h = hashlib.sha1(key.encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"osm_{h}.json")

def fetch_bbox_roads(south, west, north, east, offline=False):
    """Return OSM highway 'way' elements in the bbox. Cached on disk."""
    query = (f"[out:json][timeout:60];"
             f"way({south:.6f},{west:.6f},{north:.6f},{east:.6f})[highway];"
             f"out geom tags;")
    cache = _cache_path(query)

    if os.path.exists(cache):
        try:
            with open(cache, "r", encoding="utf-8") as f:
                print(f"    (using cached OSM data: {os.path.basename(cache)})")
                return json.load(f).get("elements", [])
        except Exception:
            pass

    if offline:
        print("    OFFLINE and no cache for this area -> no roads.")
        return []

    data = urllib.parse.urlencode({"data": query}).encode()
    for mirror in OVERPASS_MIRRORS:
        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    mirror, data=data,
                    headers={"User-Agent": "Mozilla/5.0 (road-marking-generator/4.0)",
                             "Accept": "application/json, */*"})
                with urllib.request.urlopen(req, timeout=90) as r:
                    payload = json.load(r)
                with open(cache, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                els = payload.get("elements", [])
                print(f"    OSM: {len(els)} ways from {urllib.parse.urlparse(mirror).netloc}")
                return els
            except Exception as e:
                print(f"    Overpass {urllib.parse.urlparse(mirror).netloc} "
                      f"try {attempt+1} failed: {str(e)[:70]}")
                time.sleep(1.5 * (attempt + 1))
    print("    All Overpass mirrors failed -> no roads.")
    return []


# ==================================================================
# Road geometry helpers
# ==================================================================

def _project_point_on_polyline(pts_xy):
    """
    Project the origin (0,0) onto a polyline given in metric xy.
    Returns (seg_index, t, foot_xy, s0) where s0 is arc-length of the foot.
    """
    best = (float("inf"), 0, 0.0, np.array([0.0, 0.0]), 0.0)
    s_acc = 0.0
    for i in range(len(pts_xy) - 1):
        a = pts_xy[i]; b = pts_xy[i + 1]
        ab = b - a
        L2 = float(ab @ ab)
        if L2 == 0.0:
            continue
        t = float((-a @ ab) / L2)   # projecting point (0,0): (P-a)=-a
        t = max(0.0, min(1.0, t))
        foot = a + t * ab
        d = float(foot @ foot)
        if d < best[0]:
            seg_len = math.sqrt(L2)
            best = (d, i, t, foot, s_acc + t * seg_len)
        s_acc += math.sqrt(L2)
    return best[1], best[2], best[3], best[4]

def _arc_lengths(pts_xy):
    s = [0.0]
    for i in range(len(pts_xy) - 1):
        s.append(s[-1] + float(np.linalg.norm(pts_xy[i + 1] - pts_xy[i])))
    return s

def _point_and_tangent_at_s(pts_xy, cum_s, s):
    """Return (point_xy, unit_tangent) at arc-length s (clamped to the line)."""
    total = cum_s[-1]
    s = max(0.0, min(total, s))
    # locate the segment containing s
    for i in range(len(cum_s) - 1):
        if s <= cum_s[i + 1] or i == len(cum_s) - 2:
            a = pts_xy[i]; b = pts_xy[i + 1]
            seg = b - a
            seg_len = cum_s[i + 1] - cum_s[i]
            if seg_len <= 1e-9:
                tan = seg
            else:
                tan = seg / seg_len
            local = s - cum_s[i]
            pt = a + tan * local if seg_len > 1e-9 else a
            n = np.linalg.norm(tan)
            tan = tan / n if n > 1e-9 else np.array([1.0, 0.0])
            return pt, tan
    return pts_xy[-1], np.array([1.0, 0.0])


# ==================================================================
# Road selection  +  width estimation
# ==================================================================

def _way_nodes_xy(frame, el):
    g = el.get("geometry", [])
    return np.array([frame.to_xy(p["lon"], p["lat"]) for p in g])

def choose_road(frame, elements):
    """
    Pick the road way whose centreline passes closest to the point (origin of
    frame), preferring driveable classes over footways/paths. Returns
    (element, nodes_xy, dist_m) or (None, None, inf).
    """
    def rank(el):
        hw = el.get("tags", {}).get("highway", "")
        if hw in ROAD_IMPORTANCE: return ROAD_IMPORTANCE[hw]
        if hw in LAST_RESORT:     return 100 + LAST_RESORT.index(hw)
        return 50

    best = (float("inf"), None, None)
    for el in elements:
        hw = el.get("tags", {}).get("highway", "")
        if hw not in DRIVEABLE and hw not in LAST_RESORT:
            continue
        nodes = _way_nodes_xy(frame, el)
        if len(nodes) < 2:
            continue
        _, _, foot, _ = _project_point_on_polyline(nodes)
        d = float(np.linalg.norm(foot))
        # small penalty for lower-class roads so a nearby big road is preferred
        # over a driveway that happens to be a few cm closer
        adjusted = d + rank(el) * 0.4
        if adjusted < best[0]:
            best = (adjusted, el, nodes)
    if best[1] is None:
        return None, None, float("inf")
    # report the TRUE distance, not the ranked one
    _, _, foot, _ = _project_point_on_polyline(best[2])
    return best[1], best[2], float(np.linalg.norm(foot))

# Width embedded in a placemark name. With an explicit marker (w= / width=) the
# 'm' unit is optional ("w=12"); a bare number must carry the unit ("12m") so we
# don't grab unrelated numbers in a name.
_WIDTH_MARKED = re.compile(r"(?:w|width)\s*[:=]\s*(\d{1,2}(?:\.\d+)?)\s*m?\b",
                           re.IGNORECASE)
_WIDTH_BARE   = re.compile(r"(?<![\w.])(\d{1,2}(?:\.\d+)?)\s*m\b", re.IGNORECASE)

def estimate_width(tags, name, index):
    """
    Return (width_m, source_str). Priority:
      manual override (by name/index)  >  width embedded in name  >
      OSM width tag  >  OSM lanes x LANE_WIDTH  >  road-class default.
    """
    # 1. explicit manual overrides
    for key in (name, str(index)):
        if key in MANUAL_WIDTHS:
            return float(MANUAL_WIDTHS[key]), "manual"

    # 2. width embedded in the placemark name (e.g. "Gate w=12" or "12m")
    m = _WIDTH_MARKED.search(name or "") or _WIDTH_BARE.search(name or "")
    if m:
        w = float(m.group(1))
        if 2.0 <= w <= 60.0:
            return w, "name"

    # 3. OSM width tag
    if "width" in tags:
        try:
            w = float(re.sub(r"[^\d.]", "", tags["width"]))
            if 2.0 <= w <= 60.0:
                return w, "osm:width"
        except ValueError:
            pass

    # 4. OSM lane count (+ shoulder allowance so the carriageway isn't undersized)
    if "lanes" in tags:
        try:
            lanes = float(str(tags["lanes"]).split(";")[0])
            if lanes >= 1:
                hw = tags.get("highway", "")
                shoulder = SHOULDER_TOTAL.get(hw, 0.0)
                return lanes * LANE_WIDTH + shoulder, f"lanes({int(lanes)})+shoulder"
        except ValueError:
            pass

    # 5. road-class default (link/ramp classes already have their own entries)
    hw = tags.get("highway", "_default")
    return ROAD_WIDTH_DEFAULTS.get(hw, ROAD_WIDTH_DEFAULTS["_default"]), f"default:{hw}"


# ==================================================================
# Polygon construction  (the curve-following ribbon)
# ==================================================================

def _min_turn_radius(nodes_xy, cum_s, s_lo, s_hi):
    """
    Minimum centreline turn radius (metres) over [s_lo, s_hi], via Menger
    curvature on a clean uniform resample. Returns +inf for a straight road.
    """
    total = cum_s[-1]
    s_lo = max(0.0, s_lo); s_hi = min(total, s_hi)
    if s_hi - s_lo < 1e-6:
        return float("inf")
    n = max(3, int((s_hi - s_lo) / 0.5) + 1)
    pts = [_point_and_tangent_at_s(nodes_xy, cum_s, s)[0]
           for s in np.linspace(s_lo, s_hi, n)]
    rmin = float("inf")
    for i in range(1, len(pts) - 1):
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        ab = float(np.linalg.norm(b - a))
        bc = float(np.linalg.norm(c - b))
        ca = float(np.linalg.norm(a - c))
        if ab < 1e-6 or bc < 1e-6 or ca < 1e-6:
            continue
        area2 = abs((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
        if area2 < 1e-12:
            continue                       # collinear -> straight here
        radius = (ab * bc * ca) / (2.0 * area2)   # circumradius = 1/Menger curvature
        rmin = min(rmin, radius)
    return rmin

def _rdp(pts, tol):
    """Douglas-Peucker simplify of an open polyline (list of xy arrays).
    Collapses straight runs while keeping curvature within `tol` metres."""
    if len(pts) < 3:
        return pts
    a, b = pts[0], pts[-1]
    ab = b - a
    L = float(np.linalg.norm(ab))
    if L < 1e-9:
        dists = [float(np.linalg.norm(p - a)) for p in pts]
    else:
        dists = [abs((ab[0] * (p[1] - a[1]) - ab[1] * (p[0] - a[0])) / L) for p in pts]
    imax = int(np.argmax(dists))
    if dists[imax] > tol:
        left = _rdp(pts[:imax + 1], tol)
        right = _rdp(pts[imax:], tol)
        return left[:-1] + right
    return [a, b]

def _straight_strip(frame, foot_xy, tangent, left_hw, right_hw, length):
    """Simple rectangle: fallback when the road is too short or the ribbon folds.
    left_hw / right_hw are the offsets to each edge (may differ)."""
    n = np.array([-tangent[1], tangent[0]])
    hl = length / 2.0
    corners = [foot_xy + hl * tangent + left_hw * n,
               foot_xy - hl * tangent + left_hw * n,
               foot_xy - hl * tangent - right_hw * n,
               foot_xy + hl * tangent - right_hw * n]
    corners.append(corners[0])
    return [frame.to_lonlat(*c) for c in corners], corners

def build_polygon(frame, nodes_xy, left_hw, right_hw, length, center_s=None):
    """
    Build a ribbon polygon that follows the road for `length` metres and spans
    `left_hw` metres to the left of the point and `right_hw` to the right
    (left = 90 deg CCW of travel). Asymmetric offsets let the polygon reach both
    real road edges even when the point isn't centred. Returns
    (ring_lonlat, ring_xy, is_curved).
    """
    cum_s = _arc_lengths(nodes_xy)
    total = cum_s[-1]
    seg_i, t, foot, s0 = _project_point_on_polyline(nodes_xy)

    if center_s is None:
        center_s = s0

    half = length / 2.0
    s_lo, s_hi = center_s - half, center_s + half

    # Centre the polygon on the placemark pin (the origin of the local frame),
    # keeping the orientation aligned with the road geometry.
    shift = -foot

    # Degenerate / very short way -> straight strip using local tangent.
    _, tan0 = _point_and_tangent_at_s(nodes_xy, cum_s, s0)
    if total < 1e-6:
        return (*_straight_strip(frame, foot + shift, np.array([1.0, 0.0]),
                                 left_hw, right_hw, length), False)

    # Sample arc-lengths: fine even spacing + every real vertex inside the
    # window (so bends are preserved exactly), clamped to the road's extent.
    step = max(0.25, length / 24.0)
    samples = list(np.arange(s_lo, s_hi + 1e-9, step))
    for sv in cum_s:
        if s_lo < sv < s_hi:
            samples.append(sv)
    samples = sorted(set(min(max(s, 0.0), total) for s in samples))

    left, right = [], []
    for s in samples:
        pt, tan = _point_and_tangent_at_s(nodes_xy, cum_s, s)
        pt = pt + shift
        n = np.array([-tan[1], tan[0]])
        left.append(pt + left_hw * n)
        right.append(pt - right_hw * n)

    # Validity: on a genuine hairpin the inner offset edge inverts (half-width
    # exceeds the local turn radius) and the ribbon folds. Unusable -> fall back
    # to a clean rectangle centred on the point.
    if SELF_ISECT_FALLBACK and max(left_hw, right_hw) > _min_turn_radius(nodes_xy, cum_s, s_lo, s_hi):
        ring_lonlat, rect_xy = _straight_strip(frame, foot + shift, tan0,
                                               left_hw, right_hw, length)
        return ring_lonlat, rect_xy, False

    # simplify each long edge (keeps curvature to 3 cm, collapses straight runs)
    left_s = _rdp([np.asarray(p) for p in left], 0.03)
    right_s = _rdp([np.asarray(p) for p in right], 0.03)
    ring_xy = left_s + right_s[::-1] + [left_s[0]]

    # "curved" = the road turns appreciably across the sampling window
    _, tan_lo = _point_and_tangent_at_s(nodes_xy, cum_s, max(s_lo, 0.0))
    _, tan_hi = _point_and_tangent_at_s(nodes_xy, cum_s, min(s_hi, total))
    turn_deg = math.degrees(math.acos(max(-1.0, min(1.0, float(tan_lo @ tan_hi)))))
    is_curved = turn_deg > 2.0

    ring_lonlat = [frame.to_lonlat(*p) for p in ring_xy]
    return ring_lonlat, ring_xy, is_curved

def polygon_area_m2(ring_xy):
    """Shoelace area (m^2) of the metric-space ring (accurate even when curved)."""
    x = np.array([p[0] for p in ring_xy])
    y = np.array([p[1] for p in ring_xy])
    return abs(np.dot(x[:-1], y[1:]) - np.dot(x[1:], y[:-1])) / 2.0


# ==================================================================
# KML in / out
# ==================================================================

def parse_kml_string(raw):
    """Parse KML text -> list of (lon, lat, name). Tolerant of namespaces /
    MultiGeometry / either quote style. Raises ValueError on malformed XML."""
    raw = re.sub(r'\sxmlns(:\w+)?=(["\']).*?\2', "", raw) # drop namespace declarations (either quote)
    raw = re.sub(r"<(/?)\w+:", r"<\1", raw)               # strip prefixes on element tags
    raw = re.sub(r'(\s)\w+:(\w[\w.\-]*\s*=)', r"\1\2", raw)  # strip prefixes on attribute names
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ValueError(f"KML parse error: {e}")

    records = []
    for pm in root.iter("Placemark"):
        name_el = pm.find("name")
        name = (name_el.text or "").strip() if name_el is not None else ""
        if not name:
            name = "Unnamed"
        # every Point in this placemark (usually one)
        for pt in pm.iter("Point"):
            coords_el = pt.find("coordinates")
            if coords_el is None or not coords_el.text:
                continue
            for chunk in coords_el.text.strip().split():
                parts = chunk.split(",")
                try:
                    lon, lat = float(parts[0]), float(parts[1])
                    records.append((lon, lat, name))
                except (ValueError, IndexError):
                    continue
    return records

def parse_kml_or_kmz_bytes(data):
    """Parse either a KML or a KMZ upload (raw bytes) -> list of (lon, lat, name).

    A KMZ is a zip whose main document is a .kml (usually doc.kml). Google Earth
    exports finalised files as KMZ, so the workflow must accept both. Raises
    ValueError on malformed / empty input.
    """
    # KMZ files are zip archives; their signature is 'PK\x03\x04'.
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = z.namelist()
                # Prefer doc.kml, else the first .kml entry in the archive.
                kml_name = next((n for n in names if n.lower() == "doc.kml"), None) \
                    or next((n for n in names if n.lower().endswith(".kml")), None)
                if kml_name is None:
                    raise ValueError("KMZ contains no .kml document.")
                raw = z.read(kml_name).decode("utf-8", errors="replace")
        except zipfile.BadZipFile:
            raise ValueError("File looks like a KMZ but is not a valid zip archive.")
    else:
        raw = data.decode("utf-8", errors="replace")
    return parse_kml_string(raw)


def parse_kml(filepath):
    """Return list of (lon, lat, name) from a KML file on disk."""
    with open(filepath, "r", encoding="utf-8") as f:
        raw = f.read()
    try:
        return parse_kml_string(raw)
    except ValueError as e:
        print(f"{e} in {filepath}")
        return []


# KML colours are aabbggrr (alpha, blue, green, red).
STYLE_NORMAL_FILL = "66ff8800"   # translucent orange
STYLE_NORMAL_LINE = "ffff8800"   # solid orange
STYLE_WARN_FILL   = "660000ff"   # translucent red  (flagged: no road / far)
STYLE_WARN_LINE   = "ff0000ff"   # solid red

def poly_kml(name, ring_lonlat, warn=False, description=""):
    fill = STYLE_WARN_FILL if warn else STYLE_NORMAL_FILL
    line = STYLE_WARN_LINE if warn else STYLE_NORMAL_LINE

    if not ring_lonlat:
        rings = []
    elif isinstance(ring_lonlat[0][0], (float, int)):
        # Single ring: [(lon, lat), ...]
        rings = [ring_lonlat]
    else:
        # Multiple rings: [ [(lon, lat), ...], [(lon, lat), ...] ]
        rings = ring_lonlat

    polys_xml = []
    for r in rings:
        c_str = " ".join(f"{pt[0]:.8f},{pt[1]:.8f},0" for pt in r)
        polys_xml.append(
            f"      <Polygon><outerBoundaryIs><LinearRing>\n"
            f"        <coordinates>{c_str}</coordinates>\n"
            f"      </LinearRing></outerBoundaryIs></Polygon>"
        )

    desc_xml = f"    <description>{_xml_escape(description)}</description>\n" if description else ""
    if len(polys_xml) == 1:
        geom_xml = polys_xml[0].strip()
    else:
        geom_xml = "    <MultiGeometry>\n" + "\n".join(polys_xml) + "\n    </MultiGeometry>"

    return (
        f"  <Placemark>\n"
        f"    <name>{_xml_escape(name)}</name>\n"
        f"{desc_xml}"
        f"    <Style>\n"
        f"      <LineStyle><color>{line}</color><width>2</width></LineStyle>\n"
        f"      <PolyStyle><color>{fill}</color></PolyStyle>\n"
        f"    </Style>\n"
        f"    {geom_xml}\n"
        f"  </Placemark>")

def point_kml(name, lon, lat):
    return (f"  <Placemark>\n"
            f"    <name>{_xml_escape(name)}</name>\n"
            f"    <Point><coordinates>{lon:.8f},{lat:.8f},0</coordinates></Point>\n"
            f"  </Placemark>")

def _xml_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ==================================================================
# Main pipeline
# ==================================================================

def load_width_overrides(path):
    """
    Read a CSV of per-point widths (the widths YOU provide). Flexible columns:
    a width column (any header containing 'width'), plus an index column
    ('S.No'/'index'/'no') and/or a name column ('Placemark'/'name'). Rows are
    matched to points by index first, then by name. Returns (by_index, by_name).
    """
    import csv
    by_index, by_name = {}, {}
    if not path or not os.path.exists(path):
        return by_index, by_name
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return by_index, by_name
        cols = {c.lower().strip(): c for c in reader.fieldnames}
        wcol = next((cols[k] for k in cols if "width" in k), None)
        icol = next((cols[k] for k in cols if k in ("s.no", "sno", "index", "no", "#", "sr", "sr.no")), None)
        ncol = next((cols[k] for k in cols if "placemark" in k or k == "name"), None)
        if wcol is None:
            print(f"    (widths file has no 'width' column - ignored)")
            return by_index, by_name
        for i, row in enumerate(reader, 1):
            try:
                w = float(str(row.get(wcol, "")).strip())
            except ValueError:
                continue
            if not (1.0 <= w <= 80.0):
                continue
            idx = None
            if icol:
                try: idx = int(float(row[icol]))
                except (ValueError, TypeError): idx = None
            by_index[idx if idx is not None else i] = w
            if ncol and (row.get(ncol) or "").strip():
                by_name[row[ncol].strip()] = w
    print(f"    Loaded {len(by_index)} width(s) from {path}")
    return by_index, by_name

def write_widths_template(path, boq):
    """Write an editable per-point widths CSV. Fill 'Width_m' and pass it back
    via --widths to re-run with your exact widths."""
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["S.No", "Placemark", "Lat", "Lon", "Road type", "Width_m"])
        for b in boq:
            w.writerow([b["S.No"], b["Placemark"], b["Lat"], b["Lon"],
                        b["Road type"], b["Width (m)"]])


def make_elements_fetcher(points, offline=False, log=print):
    """Return get_elements(lat, lon) -> OSM ways. Compact area -> one cached
    bbox query shared by all points; scattered -> a small cached query per point."""
    lons = [p[0] for p in points]; lats = [p[1] for p in points]
    lat_c = sum(lats) / len(lats)
    span_ns = (max(lats) - min(lats)) * 111_320.0
    span_ew = (max(lons) - min(lons)) * 111_320.0 * math.cos(math.radians(lat_c))
    if max(span_ns, span_ew) <= MAX_BBOX_M:
        dlat = BBOX_MARGIN_M / 111_320.0
        dlon = BBOX_MARGIN_M / (111_320.0 * math.cos(math.radians(lat_c)))
        shared = fetch_bbox_roads(min(lats) - dlat, min(lons) - dlon,
                                  max(lats) + dlat, max(lons) + dlon, offline=offline)
        return lambda lat, lon: shared
    log(f"    Points span {max(span_ns, span_ew)/1000:.1f} km -> querying per point.")

    def per_point(lat, lon):
        r = PER_POINT_RADIUS_M
        dlat = r / 111_320.0
        dlon = r / (111_320.0 * math.cos(math.radians(lat)))
        return fetch_bbox_roads(lat - dlat, lon - dlon, lat + dlat, lon + dlon, offline=offline)
    return per_point

def road_bearing_at(nodes_xy):
    """Compass bearing (deg, folded to [0,180)) of the road at the point's foot."""
    cum = _arc_lengths(nodes_xy)
    _, _, _, s0 = _project_point_on_polyline(nodes_xy)
    _, tan = _point_and_tangent_at_s(nodes_xy, cum, s0)
    return math.degrees(math.atan2(tan[0], tan[1])) % 180

def _full_bearing_at(nodes_xy):
    """Travel bearing (deg, 0..360) in the way's node order (= direction of travel
    for a oneway road)."""
    cum = _arc_lengths(nodes_xy)
    _, _, _, s0 = _project_point_on_polyline(nodes_xy)
    _, tan = _point_and_tangent_at_s(nodes_xy, cum, s0)
    return math.degrees(math.atan2(tan[0], tan[1])) % 360

def _is_oneway(el):
    return el.get("tags", {}).get("oneway") in ("yes", "true", "1", "-1")

def median_caps(frame, own_el, bearing, nodes_own, elements):
    """
    For a divided road, return (cap_left, cap_right): how far the width search may
    go on each side before it would cross the median into the OPPOSING carriageway.
    Only a parallel oneway way running the OPPOSITE direction counts as the median
    (so same-direction parallel ramps don't falsely cap). inf where no median.
    """
    if own_el is None or not _is_oneway(own_el):
        return float("inf"), float("inf")
    fb_own = _full_bearing_at(nodes_own)
    br = math.radians(bearing)
    n_left = np.array([-math.cos(br), math.sin(br)])     # left normal (E,N)
    cl = cr = float("inf")
    for el in elements:
        if el.get("id") == own_el.get("id"):
            continue
        t = el.get("tags", {})
        if t.get("highway", "") not in DRIVEABLE or not _is_oneway(el):
            continue
        nd = _way_nodes_xy(frame, el)
        if len(nd) < 2:
            continue
        if abs(((fb_own - _full_bearing_at(nd)) % 360) - 180) > 35:
            continue                                     # not opposite -> not a median pair
        _, _, foot, _ = _project_point_on_polyline(nd)
        signed = float(foot @ n_left)
        d = abs(signed)
        if d < 6 or d > 40:
            continue
        if signed > 0: cl = min(cl, d / 2 + 1.0)
        else:          cr = min(cr, d / 2 + 1.0)
    return cl, cr


def _measure_road_width(frame, el, nodes_xy, elements, lat, lon, class_width):
    """Measure the total road width from satellite imagery (edges detected on
    both sides of the point, capped at the median on divided roads). Returns
    (width_m, conf) or None when it can't be trusted: imagery/library
    unavailable, low confidence, or a total implausible for the road class
    (guards against over-reach onto shoulders/dirt or into vegetation)."""
    try:
        # pyrefly: ignore [missing-import]
        import roadwidth
    except Exception:
        return None
    bearing = road_bearing_at(nodes_xy)
    cap_l, cap_r = median_caps(frame, el, bearing, nodes_xy, elements)
    try:
        res = roadwidth.measure(lat, lon, bearing, cap_left=cap_l, cap_right=cap_r)
    except Exception:
        return None
    if res is None:
        return None
    left_hw, right_hw, conf = res
    total = left_hw + right_hw
    if conf < WIDTH_MIN_CONF:
        return None
    if not (0.6 * class_width <= total <= 1.8 * class_width):
        return None                          # detection implausible vs road class
    return total, conf


def build_polygons(points, mark_length=MARK_LENGTH, offline=False,
                   ov_index=None, ov_name=None, default_width=None,
                   keep_points=True, doc_name="Road Markings", rate=RATE_PER_SQM,
                   speed_breaker="",
                   speed_quantity=0.40,
                   speed_gap=0.60,
                   speed_count=1,
                   road_text="",
                   log=print):
    """
    Core pipeline: turn marking points into (output_kml_string, boq_list).
    No file I/O - usable both from the CLI and the web app. `log` is a callable
    for progress messages (pass a no-op to silence).
    """
    ov_index = ov_index or {}
    ov_name = ov_name or {}

    def resolve_width(tags, name, idx):
        """Priority: provided widths (by index/name) > name tag / MANUAL_WIDTHS /
        OSM > project default > road-class default."""
        if idx in ov_index:            return ov_index[idx], "provided"
        if name in ov_name:            return ov_name[name], "provided"
        w, src = estimate_width(tags, name, idx)
        if src.startswith("default") and default_width is not None:
            return default_width, "project-default"
        return w, src

    get_elements = make_elements_fetcher(points, offline, log)

    kml = ["<?xml version='1.0' encoding='UTF-8'?>",
           "<kml xmlns='http://www.opengis.net/kml/2.2'>",
           f"<Document><name>{_xml_escape(doc_name)}</name>"]
    boq = []

    sb_type = (speed_breaker or "").strip()
    has_speed_breaker = bool(sb_type) and speed_count >= 1

    if has_speed_breaker:
        total_span = (speed_quantity * speed_count) + (speed_gap * max(0, speed_count - 1))
    else:
        total_span = mark_length

    for idx, (lon, lat, name) in enumerate(points, 1):
        frame = LocalFrame(lat, lon)
        elements = get_elements(lat, lon)
        el, nodes_xy, dist = choose_road(frame, elements)

        warn = False
        if el is None:
            width, wsrc = resolve_width({}, name, idx)
            if wsrc.startswith("default"):
                wsrc = "no_road:default"
                warn = True                      # only flag when width is a pure guess
            left_hw = right_hw = width / 2.0
            hw_type, road_name, dist = "-", "-", float("nan")
            curved = False

            if has_speed_breaker:
                strip_rings = []
                strip_rings_xy = []
                s_start = - (total_span / 2.0)
                tan0 = np.array([1.0, 0.0])
                foot0 = np.array([0.0, 0.0])
                for i in range(speed_count):
                    s_center_offset = s_start + i * (speed_quantity + speed_gap) + (speed_quantity / 2.0)
                    strip_foot = foot0 + s_center_offset * tan0
                    s_ring_lonlat, s_ring_xy = _straight_strip(frame, strip_foot, tan0, left_hw, right_hw, speed_quantity)
                    strip_rings.append(s_ring_lonlat)
                    strip_rings_xy.append(s_ring_xy)
                ring_lonlat_for_kml = strip_rings
                area = sum(polygon_area_m2(r) for r in strip_rings_xy)
                ring = strip_rings[0]
            else:
                ring, ring_xy = _straight_strip(
                    frame, np.array([0.0, 0.0]), np.array([1.0, 0.0]),
                    left_hw, right_hw, total_span)
                ring_lonlat_for_kml = [ring]
                area = polygon_area_m2(ring_xy)
        else:
            tags = el.get("tags", {})
            hw_type = tags.get("highway", "?")
            road_name = tags.get("name", "-")
            width, wsrc = resolve_width(tags, name, idx)

            if MEASURE_WIDTH and wsrc != "provided" and not offline:
                measured = _measure_road_width(frame, el, nodes_xy, elements, lat, lon, width)
                if measured is not None:
                    width, conf = measured
                    wsrc = f"satellite(c={conf:.2f})"

            hw = width / 2.0
            if wsrc != "provided" and dist > FAR_ROAD_WARN:
                warn = True

            if has_speed_breaker:
                cum_s = _arc_lengths(nodes_xy)
                _, _, _, s0 = _project_point_on_polyline(nodes_xy)
                s_start = s0 - (total_span / 2.0)
                strip_rings = []
                strip_rings_xy = []
                curved = False
                for i in range(speed_count):
                    s_i = s_start + i * (speed_quantity + speed_gap) + (speed_quantity / 2.0)
                    s_ring_lonlat, s_ring_xy, s_curved = build_polygon(
                        frame, nodes_xy, hw, hw, speed_quantity, center_s=s_i
                    )
                    strip_rings.append(s_ring_lonlat)
                    strip_rings_xy.append(s_ring_xy)
                    if s_curved:
                        curved = True
                ring_lonlat_for_kml = strip_rings
                area = sum(polygon_area_m2(r) for r in strip_rings_xy)
                # Outer ring for satellite crop / corners
                ring, _, _ = build_polygon(frame, nodes_xy, hw, hw, total_span, center_s=s0)
            else:
                ring, ring_xy, curved = build_polygon(frame, nodes_xy, hw, hw, total_span)
                ring_lonlat_for_kml = [ring]
                area = polygon_area_m2(ring_xy)

        display_name = config.item_name(name)
        pm_title = f"{display_name} ({sb_type})" if has_speed_breaker else (f"{display_name} ({road_text})" if road_text else display_name)
        desc = (
            f"Speed Breaker Type: {sb_type}\nStrips: {speed_count} x {speed_quantity}m\nGap: {speed_gap}m\nTotal Span: {round(total_span,2)}m"
            if has_speed_breaker else (f"Text: {road_text}" if road_text else "")
        )
        kml.append(poly_kml(pm_title, ring_lonlat_for_kml, warn=warn, description=desc))
        log(f"  [{idx}/{len(points)}] {display_name} ({name}): {hw_type} '{road_name}'  "
            f"width={width:.1f}m ({wsrc})  dist={dist:.1f}m  "
            f"area={area:.1f}m2{'  [curved]' if curved else ''}")

        boq_row = {
            "S.No": idx, "Placemark": display_name,
            "Lat": round(lat, 7), "Lon": round(lon, 7),
            "Road type": hw_type, "Road name": road_name,
            "Snap dist (m)": round(dist, 2) if dist == dist else "",
            "Width (m)": round(width, 2), "Width source": wsrc,
            "Mark length (m)": round(total_span, 2),
            "Curved": "yes" if curved else "no",
            "Area (m2)": round(area, 2),
            "Rate (Rs/m2)": rate,
            "Amount (Rs)": round(area * rate, 2),
            "Flag": "CHECK" if warn else "",   # only real issues (no road / far snap)
            "_corners": ring,
        }
        if has_speed_breaker:
            boq_row["Speed Breaker"] = f"{sb_type} ({speed_count}x{speed_quantity}m, gap {speed_gap}m)"
        boq.append(boq_row)

    if keep_points:
        kml.append("  <Folder><name>Original points</name>")
        for lon, lat, name in points:
            kml.append(point_kml(name, lon, lat))
        kml.append("  </Folder>")

    kml += ["</Document>", "</kml>"]
    return "\n".join(kml), boq


def export_excel_boq(boq, output_target):
    """
    Generate the Excel BOQ workbook with satellite screenshots.
    output_target can be a filepath (string) or a file-like object (BytesIO).
    """
    wb = openpyxl.Workbook()
    ws = wb.active

    if not boq:
        wb.save(output_target)
        return

    excel_keys = [k for k in boq[0].keys() if k != "_corners"]
    headers    = excel_keys + ["Satellite View"]
    for ci, h in enumerate(headers, 1):
        ws.cell(row=1, column=ci, value=h)

    img_col = len(headers)
    img_ltr = openpyxl.utils.get_column_letter(img_col)
    ws.column_dimensions[img_ltr].width = 45

    IMG_W, IMG_H = 200, 200

    for ri, row_data in enumerate(boq, start=2):
        for ci, key in enumerate(excel_keys, 1):
            ws.cell(row=ri, column=ci, value=row_data[key])

        ws.row_dimensions[ri].height = 150

        print(f"    Fetching satellite image for row {ri}...", end=" ", flush=True)
        img = get_satellite_image(row_data["_corners"])

        if img is not None:
            img_path = os.path.join(SCREENSHOTS_DIR, f"row_{ri}.png")
            img.save(img_path)
            xl_img        = XLImage(img_path)
            xl_img.width  = IMG_W
            xl_img.height = IMG_H
            ws.add_image(xl_img, img_ltr + str(ri))
            print("done")
        else:
            ws.cell(row=ri, column=img_col, value="Image unavailable")
            print("failed")

    wb.save(output_target)


def process_kml(input_file, mark_length=MARK_LENGTH, offline=False,
                keep_points=True, widths_file=None, default_width=None):
    if not os.path.exists(input_file):
        print(f"File not found: {input_file}")
        return

    base = os.path.splitext(os.path.basename(input_file))[0]
    out_kml     = os.path.join(OUTPUT_DIR, f"out_{base}.kml")
    out_excel   = os.path.join(OUTPUT_DIR, f"BOQ_{base}.xlsx")
    out_widths  = os.path.join(OUTPUT_DIR, f"widths_{base}.csv")

    points = parse_kml(input_file)
    print(f"\nFound {len(points)} marking point(s) in {input_file}")
    if not points:
        return

    ov_index, ov_name = load_width_overrides(widths_file)
    kml_str, boq = build_polygons(points, mark_length, offline, ov_index, ov_name,
                                  default_width, keep_points,
                                  doc_name=f"Road Markings - {base}")

    with open(out_kml, "w", encoding="utf-8") as f:
        f.write(kml_str)

    # ── Excel ─────────────────────────────────────────────────
    export_excel_boq(boq, out_excel)
    df = pd.DataFrame([{k: v for k, v in r.items() if k != "_corners"} for r in boq])
    if not (widths_file and os.path.abspath(widths_file) == os.path.abspath(out_widths)):
        write_widths_template(out_widths, boq)   # don't clobber a file passed via --widths

    flagged = sum(1 for b in boq if b["Flag"])
    measured = sum(1 for b in boq if str(b["Width source"]).startswith("satellite"))
    estimated = sum(1 for b in boq if str(b["Width source"]).startswith(("default", "lanes", "no_road")))
    print(f"\n  KML     -> {out_kml}")
    print(f"  Excel   -> {out_excel}")
    print(f"  Widths  -> {out_widths}")
    print(f"  Width   : {measured} measured from imagery, {estimated} estimated from OSM")
    print(f"  Total area  : {df['Area (m2)'].sum():.2f} m2")
    print(f"  Total amount: Rs. {df['Amount (Rs)'].sum():,.2f}")
    if flagged:
        print(f"  ** {flagged} polygon(s) FLAGGED red in the KML - review in Google Earth.")


# ==================================================================
# IMAGE-MARKER WORKFLOW  (placeholder codes -> image KMZ -> grouped BOQ)
# ------------------------------------------------------------------
# New workflow requested by the road-infrastructure company:
#   1. import a KML whose placemark names are placeholder codes (A, B, ...),
#   2. output a KMZ that places the mapped IMAGE at each placemark (no polygons)
#      as a draggable point icon, so the client can review/move/add/delete,
#   3. re-import the finalised KML and generate a grouped, per-patch Excel BOQ.
# All code/item/image/cost mapping lives in config.py (single source of truth).
# ==================================================================

def cluster_patches(points, distance_m=None):
    """
    Split points into geographic patches (e.g. Surat / Pune / Mumbai) by single
    linkage: two points are in the same patch if within `distance_m` of each
    other (transitively). Returns a list of patch labels ("Patch 1", ...), one
    per input point, ordered so the first-seen patch is "Patch 1".

    A single work area collapses to one patch; scattered areas split cleanly.
    """
    if distance_m is None:
        distance_m = config.PATCH_CLUSTER_DISTANCE_M
    n = len(points)
    if n == 0:
        return []

    # Union-find over points, joined when their great-circle distance <= threshold.
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)

    def dist_m(a, b):
        lon1, lat1 = a[0], a[1]
        lon2, lat2 = b[0], b[1]
        lat0 = math.radians((lat1 + lat2) / 2.0)
        dx = (lon2 - lon1) * 111_320.0 * math.cos(lat0)
        dy = (lat2 - lat1) * 111_320.0
        return math.hypot(dx, dy)

    for i in range(n):
        for j in range(i + 1, n):
            if dist_m(points[i], points[j]) <= distance_m:
                union(i, j)

    # Number patches by first appearance so labels are stable and readable.
    label_of_root, next_id, labels = {}, 1, []
    for i in range(n):
        r = find(i)
        if r not in label_of_root:
            label_of_root[r] = f"Patch {next_id}"
            next_id += 1
        labels.append(label_of_root[r])
    return labels


# KML colours are aabbggrr. Used to flag unknown placeholder codes in red.
_STYLE_UNKNOWN_LABEL = "ff0000ff"


def build_image_kmz(points, doc_name="Road Markings", keep_missing=True, offline=False, mark_length=MARK_LENGTH, log=print):
    """
    Turn placeholder points into a KMZ that places the mapped IMAGE at each
    placemark as a single road-aligned GroundOverlay (aligned with road width and
    direction). ONE sticker per placemark — no extra point icon. Images are
    embedded in the KMZ under files/.

    Returns (kmz_bytes, summary) where summary is a list of per-point dicts
    (code, item, image, known) useful for the UI / logging.
    """
    # Gather the unique images actually used so each is embedded once.
    used_images = {}   # filename -> absolute path

    for _, _, name in points:
        entry = config.resolve(name)
        if not entry:
            continue
        fname = entry["image"]
        if fname in used_images:
            continue
        path = config.image_path(name)
        if path is None:
            log(f"    WARNING: image '{fname}' for code '{name}' not found on disk.")
            continue
        used_images[fname] = path

    # Style for unknown codes: a default pin with a red label.
    styles_xml = [
        f"  <Style id='unknown'>\n"
        f"    <IconStyle><scale>1.0</scale></IconStyle>\n"
        f"    <LabelStyle><color>{_STYLE_UNKNOWN_LABEL}</color></LabelStyle>\n"
        f"  </Style>"
    ]

    kml = ["<?xml version='1.0' encoding='UTF-8'?>",
           "<kml xmlns='http://www.opengis.net/kml/2.2' xmlns:gx='http://www.google.com/kml/ext/2.2'>",
           f"<Document><name>{_xml_escape(doc_name)}</name>"]
    kml += styles_xml

    get_elements = make_elements_fetcher(points, offline=offline, log=log)
    summary = []
    for idx, (lon, lat, name) in enumerate(points, 1):
        entry = config.resolve(name)
        known = entry is not None
        if known:
            item = entry["item"]
            fname = entry["image"]
            title = item                      # full item name as the label
        else:
            item = name
            fname = None
            title = f"{name} (UNKNOWN CODE)" if name else "(UNKNOWN CODE)"
            if not keep_missing:
                continue

        if known and fname:
            frame = LocalFrame(lat, lon)
            elements = get_elements(lat, lon)
            el, nodes_xy, dist = choose_road(frame, elements)

            if el is None:
                width, wsrc = estimate_width({}, name, idx)
                tan = np.array([1.0, 0.0])   # default: east-west
            else:
                tags = el.get("tags", {})
                width, wsrc = estimate_width(tags, name, idx)
                cum_s = _arc_lengths(nodes_xy)
                _, _, _, s0 = _project_point_on_polyline(nodes_xy)
                _, tan = _point_and_tangent_at_s(nodes_xy, cum_s, s0)

            hw = width / 2.0
            # Centre ALWAYS at the pin (0,0 in local frame); road tangent for rotation.
            ring_lonlat, corners_xy = _straight_strip(
                frame, np.array([0.0, 0.0]), tan, hw, hw, mark_length)

            # corners_xy order from _straight_strip:
            # 0: Front-Left  (map: top-left  )
            # 1: Back-Left   (map: bottom-left )
            # 2: Back-Right  (map: bottom-right)
            # 3: Front-Right (map: top-right  )
            # gx:LatLonQuad expects: bottom-left, bottom-right, top-right, top-left
            c_bl = frame.to_lonlat(*corners_xy[1])
            c_br = frame.to_lonlat(*corners_xy[2])
            c_tr = frame.to_lonlat(*corners_xy[3])
            c_tl = frame.to_lonlat(*corners_xy[0])

            coord_str = (
                f"{c_bl[0]:.8f},{c_bl[1]:.8f},0 "
                f"{c_br[0]:.8f},{c_br[1]:.8f},0 "
                f"{c_tr[0]:.8f},{c_tr[1]:.8f},0 "
                f"{c_tl[0]:.8f},{c_tl[1]:.8f},0"
            )

            # ONE GroundOverlay (image sticker on ground, road-aligned)
            # + ONE label-only Placemark so the name appears on the map
            # and the KMZ roundtrips correctly for BOQ generation.
            kml.append(
                f"  <GroundOverlay>\n"
                f"    <name>{_xml_escape(title)}</name>\n"
                f"    <Icon><href>files/{fname}</href></Icon>\n"
                f"    <gx:LatLonQuad>\n"
                f"      <coordinates>{coord_str}</coordinates>\n"
                f"    </gx:LatLonQuad>\n"
                f"  </GroundOverlay>\n"
                f"  <Placemark>\n"
                f"    <name>{_xml_escape(title)}</name>\n"
                f"    <Style>\n"
                f"      <IconStyle><scale>0</scale><Icon><href></href></Icon></IconStyle>\n"
                f"      <LabelStyle><scale>0.9</scale></LabelStyle>\n"
                f"    </Style>\n"
                f"    <Point><coordinates>{lon:.8f},{lat:.8f},0</coordinates></Point>\n"
                f"  </Placemark>"
            )
        else:
            # Unknown code: just a simple point placemark with red label
            kml.append(
                f"  <Placemark>\n"
                f"    <name>{_xml_escape(title)}</name>\n"
                f"    <styleUrl>#unknown</styleUrl>\n"
                f"    <Point><coordinates>{lon:.8f},{lat:.8f},0</coordinates></Point>\n"
                f"  </Placemark>"
            )

        summary.append({"code": config.normalize_code(name), "item": item,
                        "image": entry["image"] if known else None, "known": known})
        log(f"  [{idx}/{len(points)}] {name!r} -> {item}"
            f"{'' if known else '   [UNKNOWN CODE]'}")

    kml += ["</Document>", "</kml>"]
    kml_str = "\n".join(kml)

    # Zip: doc.kml + every used image under files/.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml_str)
        for fname, path in used_images.items():
            with open(path, "rb") as fh:
                z.writestr(f"files/{fname}", fh.read())
    return buf.getvalue(), summary


def build_grouped_boq(points, distance_m=None):
    """
    Build the grouped BOQ from finalised points.

    Groups identical items *within each geographic patch* and counts quantities.
    Placeholder codes are replaced by their full reserved item names. Returns an
    ordered dict:  patch_label -> list of row dicts, each:
        {Item, Quantity, Unit Cost, Total Cost}
    Rows are sorted by item name; Total Cost = Quantity x Unit Cost.
    """
    labels = cluster_patches(points, distance_m)
    # patch -> item -> [count, unit_cost]
    patches = {}
    for (lon, lat, name), patch in zip(points, labels):
        item = config.item_name(name)          # full name, or original if unknown
        cost = config.unit_cost(name)
        bucket = patches.setdefault(patch, {})
        if item not in bucket:
            bucket[item] = [0, cost]
        bucket[item][0] += 1

    result = {}
    for patch, items in patches.items():
        rows = []
        for item in sorted(items):
            qty, cost = items[item]
            rows.append({
                "Item": item,
                "Quantity": qty,
                "Unit Cost": round(cost, 2),
                "Total Cost": round(qty * cost, 2),
            })
        result[patch] = rows
    return result


def _safe_sheet_title(title, used):
    """Excel sheet names: <=31 chars, no []:*?/\\, and unique within the book."""
    clean = re.sub(r"[\[\]:*?/\\]", " ", str(title)).strip()[:31] or "Sheet"
    base, n = clean, 2
    while clean in used:
        suffix = f" ({n})"
        clean = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(clean)
    return clean


def export_grouped_excel(grouped, output_target):
    """
    Write the grouped BOQ to Excel: ONE worksheet per patch, each containing
    only that patch's items. Columns: Item | Quantity | Unit Cost | Total Cost,
    with a bold TOTAL row. Structured so real unit costs can simply be filled in
    later (Total Cost already = Quantity x Unit Cost).

    output_target: filepath (str) or a file-like object (e.g. BytesIO).
    """
    from openpyxl.styles import Font, Alignment, PatternFill

    wb = openpyxl.Workbook()
    wb.remove(wb.active)     # drop the default empty sheet; we add our own

    headers = ["Item", "Quantity", "Unit Cost", "Total Cost"]
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1976D2")
    total_font = Font(bold=True)
    used_titles = set()

    if not grouped:
        wb.create_sheet(_safe_sheet_title("BOQ", used_titles))

    for patch, rows in grouped.items():
        ws = wb.create_sheet(_safe_sheet_title(patch, used_titles))
        for ci, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=ci, value=h)
            c.font = header_font
            c.fill = header_fill
            c.alignment = Alignment(horizontal="center")

        r = 2
        for row in rows:
            ws.cell(row=r, column=1, value=row["Item"])
            ws.cell(row=r, column=2, value=row["Quantity"])
            ws.cell(row=r, column=3, value=row["Unit Cost"])
            ws.cell(row=r, column=4, value=row["Total Cost"])
            r += 1

        # TOTAL row (sum of quantities and costs for this patch).
        ws.cell(row=r, column=1, value="TOTAL").font = total_font
        tq = ws.cell(row=r, column=2, value=sum(x["Quantity"] for x in rows))
        tc = ws.cell(row=r, column=4, value=round(sum(x["Total Cost"] for x in rows), 2))
        tq.font = total_font
        tc.font = total_font

        # Reasonable column widths.
        ws.column_dimensions["A"].width = 32
        for col in ("B", "C", "D"):
            ws.column_dimensions[col].width = 14

    wb.save(output_target)


def main():
    ap = argparse.ArgumentParser(description="Road marking polygon generator")
    ap.add_argument("kml", nargs="+", help="input KML file(s)")
    ap.add_argument("--length", type=float, default=MARK_LENGTH,
                    help=f"marking length along the road, metres (default {MARK_LENGTH})")
    ap.add_argument("--offline", action="store_true",
                    help="use only cached OSM data, never hit the network")
    ap.add_argument("--no-points", action="store_true",
                    help="do not include the original points in the output KML")
    ap.add_argument("--widths", metavar="CSV",
                    help="CSV of per-point widths you provide (column 'Width_m'); "
                         "these override all automatic estimates")
    ap.add_argument("--default-width", type=float, metavar="M",
                    help="fallback carriageway width (m) for any point without a "
                         "provided/OSM width, instead of the road-class default")
    args = ap.parse_args()

    for f in args.kml:
        print(f"\n=== Processing: {f} ===")
        process_kml(f, mark_length=args.length, offline=args.offline,
                    keep_points=not args.no_points,
                    widths_file=args.widths, default_width=args.default_width)


if __name__ == "__main__":
    main()
