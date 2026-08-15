"""
Central mapping configuration
=============================
THE single source of truth for the placeholder workflow.

A KML exported from Google Earth contains placemarks whose names are short
placeholder *codes* (A, B, C ...). Every code maps to:

    code  ->  full item name  ->  image file  ->  unit cost

Everything downstream (the image KMZ and the Excel BOQ) reads from this file
and nothing else. To add a new marking / sign, or to change an item name, an
image, or a price, edit ONLY this file - no code changes anywhere else.

Field meaning per code
----------------------
    item      : full reserved name written into the Excel BOQ (never the code)
    image     : file inside IMAGES_DIR shown at the placemark in the output KMZ
    type      : "marking" | "sign" | "text" (informational / future grouping)
    unit_cost : price per unit. Leave 0.0 until real rates are available - the
                BOQ is already structured to multiply Quantity x Unit Cost.

NOTE ON IMAGES: only 6 images ship in images/. Codes E-H reuse the closest
available image as a placeholder; drop the correct file in images/ and update
the "image" field here when the real sign artwork is available.
"""
import os

# Folder holding the marking / sign images (relative to this file).
IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")

# ---------------------------------------------------------------------------
# THE MAPPING  -  edit here only.
# Keys are the placeholder codes as they appear in the input KML placemark name.
# ---------------------------------------------------------------------------
PLACEHOLDER_MAP = {
    "A": {"item": "CAP PTBM 20mm X 5",        "image": "1000214874.jpg", "type": "marking", "unit_cost": 0.0},
    "B": {"item": "CAP PTBM 15mm X 5",        "image": "1000214874.jpg", "type": "marking", "unit_cost": 0.0},
    "C": {"item": "CAP PTBM 10mm X 5",        "image": "1000214874.jpg", "type": "marking", "unit_cost": 0.0},
    "D": {"item": "GO SLOW",                 "image": "1000214872.jpg", "type": "text",    "unit_cost": 0.0},
    # "E": {"item": "Speed Breaker Sign Board","image": "1000214871.jpg", "type": "sign",    "unit_cost": 0.0},
    # "F": {"item": "School Ahead",            "image": "1000214870.jpg", "type": "sign",    "unit_cost": 0.0},
    "G": {"item": "GO SLOW",                 "image": "1000214872.jpg", "type": "text",    "unit_cost": 0.0},
    # "H": {"item": "Curve Right",             "image": "1000214875.jpg", "type": "sign",    "unit_cost": 0.0},
}


# Default icon pixel scale for the placed images in Google Earth.
ICON_SCALE = 1.2

# Geographic clustering: points farther apart than this (metres) start a new
# patch / worksheet. Tune for how far apart your work areas typically are.
PATCH_CLUSTER_DISTANCE_M = 2000.0


# ---------------------------------------------------------------------------
# Lookup helpers  -  the only public API other modules should use.
# ---------------------------------------------------------------------------

def normalize_code(name):
    """Turn a raw placemark name into a lookup key.

    Tolerant of surrounding whitespace and case, and of names that carry extra
    text after the code (e.g. "A - stop line" -> "A"). Returns "" if empty.
    """
    if not name:
        return ""
    token = name.strip().split()[0] if name.strip() else ""
    # keep only the leading code token, strip trailing punctuation like "A."
    token = token.strip().strip(".,:;-").strip()
    return token.upper()


# Reverse index: full item name -> its mapping entry. Lets the BOQ step resolve
# a finalised KML whose placemarks are named by the FULL ITEM NAME (which is how
# the Step-1 image KMZ labels them) and not just by the original code.
_ITEM_INDEX = {v["item"].strip().lower(): v for v in PLACEHOLDER_MAP.values()}


def resolve(name):
    """Return the mapping dict for a placemark name, or None if unknown.

    Resolves in this order so the workflow round-trips:
      1. a bare/decorated placeholder code ("A", "A - stop line"),
      2. the full reserved item name ("CAP PTBM 15mm x6"), as written into the
         Step-1 KMZ labels and thus present in the client's finalised file.
    """
    entry = PLACEHOLDER_MAP.get(normalize_code(name))
    if entry:
        return entry
    if name:
        return _ITEM_INDEX.get(name.strip().lower())
    return None


def item_name(name, default=None):
    """Full reserved item name for a placemark, or `default` (the original
    name when not given) if the code is unknown."""
    entry = resolve(name)
    if entry:
        return entry["item"]
    return default if default is not None else name


def unit_cost(name):
    """Unit cost for a placemark's item (0.0 if unknown / not yet priced)."""
    entry = resolve(name)
    return float(entry["unit_cost"]) if entry else 0.0


def image_file(name):
    """Image filename for a placemark, or None if the code is unknown."""
    entry = resolve(name)
    return entry["image"] if entry else None


def image_path(name):
    """Absolute path to the image for a placemark, or None if unknown / the
    file is missing on disk."""
    fname = image_file(name)
    if not fname:
        return None
    path = os.path.join(IMAGES_DIR, fname)
    return path if os.path.exists(path) else None


def is_known(name):
    """True if the placemark's code exists in the mapping."""
    return resolve(name) is not None
