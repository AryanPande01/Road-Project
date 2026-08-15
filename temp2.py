"""
Road Marking Polygon Generator - v4 (curve-aware, road-fitting)
================================================================
Given a KML of marked points (exported from Google Earth), draw for each point
a polygon that:
  * spans the FULL WIDTH of the road it sits on (kerb to kerb of one carriageway),
  * follows the road's SHAPE / curvature (not a flat rectangle),
  * is aligned with the road's direction and alignment.

The polygons + a BOQ (bill of quantities) spreadsheet are written out so the
result can be opened in Google Earth and used for paint-cost estimation.

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

import math, os, re, json, time, hashlib, argparse
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET
import numpy as np
import pandas as pd

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

def build_polygon(frame, nodes_xy, left_hw, right_hw, length):
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

    half = length / 2.0
    s_lo, s_hi = s0 - half, s0 + half

    # Centre the polygon on the ROAD CENTRELINE (the projection of the pin onto
    # the road), NOT on the pin itself. The pin is often a few metres off the
    # centreline; centring on the accurate OSM centreline keeps the polygon
    # symmetric on the road instead of overhanging one edge. Position comes
    # entirely from OSM (which lines up with the imagery), so it stays on the
    # road in Google Earth just like the text does.
    shift = np.array([0.0, 0.0])        # no lateral shift -> centred on centreline

    # Degenerate / very short way -> straight strip using local tangent.
    _, tan0 = _point_and_tangent_at_s(nodes_xy, cum_s, s0)
    if total < 1e-6:
        return (*_straight_strip(frame, foot, np.array([1.0, 0.0]),
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
        ring_lonlat, rect_xy = _straight_strip(frame, foot, tan0,
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

def poly_kml(name, ring_lonlat, warn=False):
    fill = STYLE_WARN_FILL if warn else STYLE_NORMAL_FILL
    line = STYLE_WARN_LINE if warn else STYLE_NORMAL_LINE
    c_str = " ".join(f"{lon:.8f},{lat:.8f},0" for lon, lat in ring_lonlat)
    return (
        f"  <Placemark>\n"
        f"    <name>{_xml_escape(name)}</name>\n"
        f"    <Style>\n"
        f"      <LineStyle><color>{line}</color><width>2</width></LineStyle>\n"
        f"      <PolyStyle><color>{fill}</color></PolyStyle>\n"
        f"    </Style>\n"
        f"    <Polygon><outerBoundaryIs><LinearRing>\n"
        f"      <coordinates>{c_str}</coordinates>\n"
        f"    </LinearRing></outerBoundaryIs></Polygon>\n"
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
            ring, ring_xy = _straight_strip(
                frame, np.array([0.0, 0.0]), np.array([1.0, 0.0]),
                left_hw, right_hw, mark_length)
            curved = False
            hw_type, road_name, dist = "-", "-", float("nan")
        else:
            tags = el.get("tags", {})
            hw_type = tags.get("highway", "?")
            road_name = tags.get("name", "-")
            width, wsrc = resolve_width(tags, name, idx)

            # Refine the width from satellite imagery when it's confident and
            # plausible (magnitude only - position comes from OSM). Falls back to
            # the OSM estimate when a width was provided, measurement is
            # disabled/offline, or the reading can't be trusted.
            if MEASURE_WIDTH and wsrc != "provided" and not offline:
                measured = _measure_road_width(frame, el, nodes_xy, elements, lat, lon, width)
                if measured is not None:
                    width, conf = measured
                    wsrc = f"satellite(c={conf:.2f})"

            hw = width / 2.0
            ring, ring_xy, curved = build_polygon(frame, nodes_xy, hw, hw, mark_length)
            if wsrc != "provided" and dist > FAR_ROAD_WARN:
                warn = True

        area = polygon_area_m2(ring_xy)
        kml.append(poly_kml(name, ring, warn=warn))
        log(f"  [{idx}/{len(points)}] {name}: {hw_type} '{road_name}'  "
            f"width={width:.1f}m ({wsrc})  dist={dist:.1f}m  "
            f"area={area:.1f}m2{'  [curved]' if curved else ''}")

        boq.append({
            "S.No": idx, "Placemark": name,
            "Lat": round(lat, 7), "Lon": round(lon, 7),
            "Road type": hw_type, "Road name": road_name,
            "Snap dist (m)": round(dist, 2) if dist == dist else "",
            "Width (m)": round(width, 2), "Width source": wsrc,
            "Mark length (m)": mark_length,
            "Curved": "yes" if curved else "no",
            "Area (m2)": round(area, 2),
            "Rate (Rs/m2)": rate,
            "Amount (Rs)": round(area * rate, 2),
            "Flag": "CHECK" if warn else "",   # only real issues (no road / far snap)
        })

    if keep_points:
        kml.append("  <Folder><name>Original points</name>")
        for lon, lat, name in points:
            kml.append(point_kml(name, lon, lat))
        kml.append("  </Folder>")

    kml += ["</Document>", "</kml>"]
    return "\n".join(kml), boq


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
    df = pd.DataFrame(boq)
    df.to_excel(out_excel, index=False)
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