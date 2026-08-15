# Road Marking Generator

Turn a KML of marked points (exported from Google Earth) into road-fitting
markings for paint-cost estimation:

- **Polygon markings** — paint strips centred on each point, aligned to the road,
  following its direction and curvature. Road **width adapts to each road** —
  measured from satellite imagery, so a wide highway and a narrow village road
  each get the right size.
- **Text markings** — a word/phrase (e.g. `GO SLOW`) painted along each road,
  aligned to the road direction and sized to the carriageway.

Each run also produces a **BOQ** (bill of quantities): area × your paint rate.

## Requirements

- Python 3.9+
- Internet access (roads from OpenStreetMap; imagery from Esri World Imagery)
- Google Earth Pro (to open the output; optional)

## Install

```bash
pip install -r requirements.txt
```

## Web app (recommended)

```bash
python app.py
```
Open <http://127.0.0.1:5000>. Upload a `.kml`, choose the paint rate, pick the
**Polygon** or **Text** tab, and Generate. Download the output (KML / KMZ) and
the BOQ, or click **Open in Google Earth** (opens the file in Google Earth Pro
on this machine).

## Command line

```bash
python temp2.py input.kml                 # polygons -> outputs/out_input.kml + BOQ
python temp2.py input.kml --length 4      # marking depth along the road (m)
python temp2.py input.kml --offline       # use only cached data, skip imagery
```

Outputs are written to `outputs/`.

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask web app (both tabs, paint rate, open-in-Google-Earth) |
| `templates/index.html` | Web UI |
| `temp2.py` | Polygon engine + CLI (roads, width, geometry, KML, BOQ) |
| `roadwidth.py` | Road-width measurement from satellite imagery |
| `textmode.py` | Text-on-road markings (KMZ ground overlays) |
| `input.kml` | Sample input |

## How width is determined

Direction, shape and curvature come from OpenStreetMap (accurate). Width is
determined per point, in this order:

1. an explicit width in the placemark name (e.g. `w=12`) or an OSM `width` tag —
   used exactly, marked `provided`;
2. **measured from satellite imagery** (`roadwidth.py`) — the road edges are
   detected on each side of the point (vegetation-aware, capped at the median on
   divided roads), giving a width that fits *this* road;
3. if the reading is low-confidence or implausible for the road class, it falls
   back to an OSM lanes-plus-shoulder / road-class estimate.

The polygon is centred on the point and symmetric, for a clean, consistent
result. The BOQ's **Width source** column shows the origin of each width
(`satellite(c=…)`, `provided`, `lanes…`, `default:…`).

> Satellite measurement is best on clear roads; on dense multi-level
> interchanges spot-check the result. To force an exact value, put `w=12` in the
> placemark name or pass a CSV with `--widths` (CLI). Set `MEASURE_WIDTH = False`
> in `temp2.py` to use OSM estimates only (no imagery, faster, offline-friendly).

## Tests

```bash
python -m unittest test_roadmark -v
```
Network-free unit tests for parsing, width logic, geometry and the pipeline.

## Configuration

Key settings at the top of `temp2.py`: `MARK_LENGTH`, `RATE_PER_SQM`,
`MEASURE_WIDTH`, `WIDTH_MIN_CONF`, `LANE_WIDTH`, `SHOULDER_TOTAL`,
`ROAD_WIDTH_DEFAULTS`. Detection tuning is in `roadwidth.py`.
