"""
Test suite for the road-marking generator.

Network-free: exercises parsing, width logic, geometry, road selection and the
end-to-end pipeline (offline) so it runs anywhere.

Run:  python -m unittest test_roadmark -v
  or: python test_roadmark.py
"""
import math
import unittest
import xml.etree.ElementTree as ET
import numpy as np

import temp2updated as temp2


def _hline(highway, north_offset_m, way_id=1, **tags):
    """A short E-W way offset `north_offset_m` metres north of (18.0, 73.0)."""
    dlat = north_offset_m / 111_320.0
    t = {"highway": highway}; t.update(tags)
    return {"id": way_id, "tags": t,
            "geometry": [{"lon": 72.999, "lat": 18.0 + dlat},
                         {"lon": 73.001, "lat": 18.0 + dlat}]}


class TestParse(unittest.TestCase):
    NS = 'xmlns="http://www.opengis.net/kml/2.2"'

    def test_basic_point(self):
        kml = f'<kml {self.NS}><Placemark><name>A</name>' \
              f'<Point><coordinates>73.1,18.2,0</coordinates></Point></Placemark></kml>'
        r = temp2.parse_kml_string(kml)
        self.assertEqual(len(r), 1)
        self.assertAlmostEqual(r[0][0], 73.1)
        self.assertAlmostEqual(r[0][1], 18.2)
        self.assertEqual(r[0][2], "A")

    def test_single_quote_xmlns_and_prefixes(self):
        kml = ("<kml xmlns='http://www.opengis.net/kml/2.2' xmlns:gx='u'>"
               "<Document><gx:Tour/><Placemark>"
               "<Point><coordinates>1.5,2.5</coordinates></Point>"
               "</Placemark></Document></kml>")
        r = temp2.parse_kml_string(kml)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0][2], "Unnamed")     # no name -> "Unnamed"

    def test_multigeometry_and_missing_z(self):
        kml = (f'<kml {self.NS}><Placemark><name>M</name><MultiGeometry>'
               f'<Point><coordinates>1,2</coordinates></Point>'
               f'<Point><coordinates>3,4</coordinates></Point>'
               f'</MultiGeometry></Placemark></kml>')
        r = temp2.parse_kml_string(kml)
        self.assertEqual(len(r), 2)

    def test_ignores_non_point_geometry(self):
        kml = (f'<kml {self.NS}><Placemark><LineString>'
               f'<coordinates>1,2 3,4</coordinates></LineString></Placemark></kml>')
        self.assertEqual(temp2.parse_kml_string(kml), [])

    def test_malformed_raises(self):
        with self.assertRaises(ValueError):
            temp2.parse_kml_string("<kml><Placemark></kaml>")


class TestWidth(unittest.TestCase):
    def test_name_override_priority(self):
        w, src = temp2.estimate_width({"highway": "service"}, "Stop w=12", 1)
        self.assertEqual(w, 12.0)
        self.assertEqual(src, "name")

    def test_osm_width_tag(self):
        w, src = temp2.estimate_width({"width": "8.5"}, "x", 1)
        self.assertEqual(w, 8.5)
        self.assertEqual(src, "osm:width")

    def test_lanes_plus_shoulder(self):
        w, src = temp2.estimate_width({"highway": "trunk", "lanes": "2"}, "x", 1)
        self.assertAlmostEqual(w, 2 * temp2.LANE_WIDTH + temp2.SHOULDER_TOTAL["trunk"])
        self.assertTrue(src.startswith("lanes"))

    def test_class_default(self):
        w, src = temp2.estimate_width({"highway": "service"}, "x", 1)
        self.assertEqual(w, temp2.ROAD_WIDTH_DEFAULTS["service"])
        self.assertEqual(src, "default:service")

    def test_implausible_width_ignored(self):
        w, src = temp2.estimate_width({"highway": "residential", "width": "999"}, "x", 1)
        self.assertEqual(src, "default:residential")   # 999 rejected by sanity range


class TestGeometry(unittest.TestCase):
    def setUp(self):
        self.frame = temp2.LocalFrame(18.0, 73.0)
        self.straight = np.array([[-60.0, 0.0], [60.0, 0.0]])

    def _area(self, ring_xy):
        return temp2.polygon_area_m2([np.asarray(p) for p in ring_xy])

    def test_straight_rectangle_area(self):
        _, ring_xy, curved = temp2.build_polygon(self.frame, self.straight, 3.5, 3.5, 4.0)
        self.assertFalse(curved)
        self.assertAlmostEqual(self._area(ring_xy), 7.0 * 4.0, delta=0.5)   # width 7 x len 4

    def test_asymmetric_offsets(self):
        _, ring_xy, _ = temp2.build_polygon(self.frame, self.straight, 2.0, 5.0, 4.0)
        self.assertAlmostEqual(self._area(ring_xy), (2.0 + 5.0) * 4.0, delta=0.5)

    def test_curve_is_followed(self):
        R = 30.0
        arc = np.array([[R * math.sin(math.radians(a)), R * math.cos(math.radians(a)) - R]
                        for a in np.linspace(-40, 40, 17)])
        _, ring_xy, curved = temp2.build_polygon(self.frame, arc, 4.0, 4.0, 20.0)
        self.assertTrue(curved)
        # ribbon area ~= width * centreline length
        self.assertAlmostEqual(self._area(ring_xy), 8.0 * 20.0, delta=10)

    def test_hairpin_folds_to_rectangle(self):
        R = 2.5
        arc = np.array([[R * math.sin(math.radians(a)), R * math.cos(math.radians(a)) - R]
                        for a in np.linspace(-90, 90, 25)])
        _, ring_xy, curved = temp2.build_polygon(self.frame, arc, 3.5, 3.5, 8.0)
        self.assertEqual(len(ring_xy), 5)      # fell back to a clean rectangle
        self.assertFalse(curved)

    def test_polygon_centred_on_point(self):
        # point is the frame origin; polygon centroid should be ~ (0,0)
        _, ring_xy, _ = temp2.build_polygon(self.frame, self.straight, 3.0, 3.0, 4.0)
        pts = np.array(ring_xy[:-1])
        self.assertLess(abs(pts[:, 0].mean()), 0.5)
        self.assertLess(abs(pts[:, 1].mean()), 0.5)

    def test_bearing_north_and_east(self):
        self.assertAlmostEqual(temp2.road_bearing_at(np.array([[0, 0], [0, 100]])) % 180, 0, delta=1)
        self.assertAlmostEqual(temp2.road_bearing_at(np.array([[0, 0], [100, 0]])) % 180, 90, delta=1)


class TestChooseRoad(unittest.TestCase):
    def setUp(self):
        self.frame = temp2.LocalFrame(18.0, 73.0)

    def test_prefers_major_within_bias(self):
        els = [_hline("service", 3.0, 1), _hline("trunk", 4.0, 2)]
        el, _, _ = temp2.choose_road(self.frame, els)
        self.assertEqual(el["tags"]["highway"], "trunk")

    def test_picks_clearly_nearest(self):
        els = [_hline("service", 1.0, 1), _hline("trunk", 30.0, 2)]
        el, _, dist = temp2.choose_road(self.frame, els)
        self.assertEqual(el["tags"]["highway"], "service")
        self.assertAlmostEqual(dist, 1.0, delta=0.5)

    def test_no_driveable_returns_none(self):
        el, nodes, dist = temp2.choose_road(self.frame, [_hline("footway", 1.0, 1)])
        # footway is last-resort but still returned; a pure-empty list returns None
        el2, _, _ = temp2.choose_road(self.frame, [])
        self.assertIsNone(el2)


class TestPipeline(unittest.TestCase):
    def test_offline_produces_valid_kml(self):
        pts = [(73.0, 18.0, "A"), (73.0009, 18.0009, "B")]
        kml, boq = temp2.build_polygons(pts, offline=True, log=lambda *a: None)
        ET.fromstring(kml)                      # must be well-formed
        self.assertEqual(len(boq), 2)
        self.assertEqual(kml.count("<Polygon>"), 2)
        for b in boq:
            self.assertGreater(b["Area (m2)"], 0)

    def test_rate_flows_to_amount(self):
        pts = [(73.0, 18.0, "A")]
        _, boq = temp2.build_polygons(pts, offline=True, rate=100, log=lambda *a: None)
        self.assertAlmostEqual(boq[0]["Amount (Rs)"], boq[0]["Area (m2)"] * 100, delta=0.01)

    def test_speed_breaker_span_and_multigeometry(self):
        pts = [(73.0, 18.0, "SpeedBreakerTest")]
        # 6 breakers x 0.40m + 5 gaps x 0.60m = 2.40 + 3.00 = 5.40m
        kml, boq = temp2.build_polygons(
            pts, offline=True, speed_breaker="PTBM 10mm",
            speed_quantity=0.40, speed_gap=0.60, speed_count=6,
            log=lambda *a: None
        )
        ET.fromstring(kml)
        self.assertEqual(len(boq), 1)
        self.assertEqual(boq[0]["Mark length (m)"], 5.40)
        self.assertIn("PTBM 10mm", boq[0]["Speed Breaker"])
        self.assertEqual(kml.count("<Polygon>"), 6)
        self.assertIn("<MultiGeometry>", kml)


class TestRoadwidthModule(unittest.TestCase):
    def test_global_px_monotonic(self):
        import roadwidth
        x1, _ = roadwidth._global_px(18.0, 73.0)
        x2, _ = roadwidth._global_px(18.0, 73.001)
        self.assertGreater(x2, x1)             # east increases pixel x


# ==================================================================
# Image-marker workflow: central mapping, KMZ output, clustering, BOQ
# (all network-free)
# ==================================================================
import io as _io
import zipfile as _zipfile
import config
import temp2updated


class TestConfigMapping(unittest.TestCase):
    def test_resolve_known_and_normalisation(self):
        # bare code, lowercase, decorated name, trailing punctuation all resolve
        self.assertEqual(config.item_name("A"), "CAP PTBM 20mm X 5")
        self.assertEqual(config.item_name("a"), "CAP PTBM 20mm X 5")
        self.assertEqual(config.item_name("A - stop line"), "CAP PTBM 20mm X 5")
        self.assertEqual(config.item_name("B."), "CAP PTBM 15mm X 5")

    def test_unknown_code(self):
        self.assertIsNone(config.resolve("ZZZ"))
        self.assertFalse(config.is_known("ZZZ"))
        # item_name falls back to the original name for unknown codes
        self.assertEqual(config.item_name("ZZZ"), "ZZZ")
        self.assertEqual(config.unit_cost("ZZZ"), 0.0)

    def test_resolve_by_full_item_name(self):
        # A finalised KML/KMZ labels placemarks by the full item name (not the
        # code) - it must still resolve back to the same entry (and its cost).
        self.assertIs(config.resolve("CAP PTBM 20mm X 5"), config.resolve("A"))
        self.assertEqual(config.item_name("cap ptbm 20mm x 5"), "CAP PTBM 20mm X 5")
        self.assertEqual(config.unit_cost("CAP PTBM 20mm X 5"), config.unit_cost("A"))

    def test_image_path_exists(self):
        # every mapped image should actually be present on disk
        for code in config.PLACEHOLDER_MAP:
            self.assertIsNotNone(config.image_path(code),
                                 f"image for code {code} missing on disk")


class TestImageKmz(unittest.TestCase):
    PTS = [(73.7680, 18.4291, "A"), (73.7681, 18.4292, "A"),
           (73.7682, 18.4293, "B"), (73.7683, 18.4294, "Z")]

    def test_kmz_structure(self):
        kmz, summary = temp2updated.build_image_kmz(self.PTS, offline=True, log=lambda *a: None)
        z = _zipfile.ZipFile(_io.BytesIO(kmz))
        names = z.namelist()
        self.assertIn("doc.kml", names)
        doc = z.read("doc.kml").decode("utf-8")
        # image markers use GroundOverlay with gx:LatLonQuad for road alignment (no extra Point icon)
        self.assertIn("<GroundOverlay>", doc)
        self.assertIn("<gx:LatLonQuad>", doc)
        # unknown code Z produces a Point placemark; known codes do NOT
        self.assertIn("<Point>", doc)
        # A and B share one image -> exactly one embedded file under files/
        embedded = [n for n in names if n.startswith("files/")]
        self.assertEqual(len(embedded), 1)
        # the full item name (not the code) is used as the label
        self.assertIn("CAP PTBM 20mm X 5", doc)

    def test_unknown_code_flagged(self):
        kmz, summary = temp2updated.build_image_kmz(self.PTS, offline=True, log=lambda *a: None)
        unknown = [s for s in summary if not s["known"]]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["code"], "Z")

    def test_kmz_roundtrips_into_boq(self):
        # The Step-1 KMZ (placemarks labelled by full item name) must parse back
        # to points and produce the same grouped BOQ as the original codes.
        codes = [(73.7680, 18.4291, "A"), (73.7681, 18.4292, "A"),
                 (73.7682, 18.4293, "B")]
        kmz, _ = temp2updated.build_image_kmz(codes, offline=True, log=lambda *a: None)
        pts = temp2updated.parse_kml_or_kmz_bytes(kmz)      # accepts KMZ bytes
        self.assertEqual(len(pts), 3)
        grouped = temp2updated.build_grouped_boq(pts)
        rows = {r["Item"]: r["Quantity"] for r in list(grouped.values())[0]}
        self.assertEqual(rows["CAP PTBM 20mm X 5"], 2)
        self.assertEqual(rows["CAP PTBM 15mm X 5"], 1)


class TestPatchClustering(unittest.TestCase):
    def test_two_distant_patches(self):
        pts = [(73.7680, 18.4291, "A"), (73.7681, 18.4292, "B"),   # Pune-ish
               (72.8311, 21.1702, "A"), (72.8312, 21.1703, "B")]   # Surat-ish
        labels = temp2updated.cluster_patches(pts)
        self.assertEqual(labels[0], labels[1])       # near points share a patch
        self.assertEqual(labels[2], labels[3])
        self.assertNotEqual(labels[0], labels[2])    # far points split
        self.assertEqual(len(set(labels)), 2)

    def test_single_area_one_patch(self):
        pts = [(73.7680, 18.4291, "A"), (73.7681, 18.4292, "B")]
        self.assertEqual(len(set(temp2updated.cluster_patches(pts))), 1)


class TestGroupedBOQ(unittest.TestCase):
    def test_grouping_and_quantities(self):
        pts = [(73.7680, 18.4291, "A"), (73.7681, 18.4292, "A"),
               (73.7682, 18.4293, "A"), (73.7683, 18.4294, "B")]
        grouped = temp2updated.build_grouped_boq(pts)
        self.assertEqual(len(grouped), 1)            # one patch
        rows = list(grouped.values())[0]
        by_item = {r["Item"]: r for r in rows}
        self.assertEqual(by_item["CAP PTBM 20mm X 5"]["Quantity"], 3)
        self.assertEqual(by_item["CAP PTBM 15mm X 5"]["Quantity"], 1)
        # cost columns present and Total = Qty x Unit Cost
        for r in rows:
            self.assertEqual(r["Total Cost"], round(r["Quantity"] * r["Unit Cost"], 2))

    def test_separate_sheets_per_patch(self):
        pts = [(73.7680, 18.4291, "A"),
               (72.8311, 21.1702, "B")]              # far apart -> 2 patches
        grouped = temp2updated.build_grouped_boq(pts)
        self.assertEqual(len(grouped), 2)
        buf = _io.BytesIO()
        temp2updated.export_grouped_excel(grouped, buf)
        buf.seek(0)
        import openpyxl
        wb = openpyxl.load_workbook(buf)
        self.assertEqual(len(wb.sheetnames), 2)      # one worksheet per patch
        # each sheet has the cost-ready header
        for ws in wb.worksheets:
            self.assertEqual([ws.cell(1, c).value for c in range(1, 5)],
                             ["Item", "Quantity", "Unit Cost", "Total Cost"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
