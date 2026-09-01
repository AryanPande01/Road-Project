"""
Text road-markings
===================
Paint a word / phrase (e.g. "GO SLOW") onto the road at each marked point,
aligned with the road direction, sized to the carriageway. Output is a KMZ
(zipped KML + one text image) that opens directly in Google Earth.

We use a KML <GroundOverlay>: the text is rendered to a transparent PNG and
draped on the ground inside a lat/lon box rotated to the road bearing. Painted
area (for the BOQ) is measured from the fraction of painted pixels.
"""
import io, os, math, zipfile
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import temp2updated as temp2

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\Arialbd.ttf",
    r"C:\Windows\Fonts\ARIALBD.TTF", r"C:\Windows\Fonts\arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
TEXT_STRETCH   = 2.0     # elongate letters along the road (road-marking style)
WIDTH_FRACTION = 0.8     # text block spans this fraction of the carriageway width
MIN_BLOCK_M    = 3.0     # minimum text-block width across the road (metres)


def _font(size):
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def render_text_image(text, fsize=140):
    """White uppercase text (words stacked on separate lines) on a transparent
    PNG. Returns (PIL.Image, painted_fraction)."""
    lines = [w for w in text.upper().split() if w] or ["?"]
    font = _font(fsize)
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    dims = [probe.textbbox((0, 0), ln, font=font) for ln in lines]
    line_w = [b[2] - b[0] for b in dims]
    line_h = [b[3] - b[1] for b in dims]
    pad = int(fsize * 0.25)
    gap = int(fsize * 0.28)
    W = max(line_w) + 2 * pad
    H = sum(line_h) + gap * (len(lines) - 1) + 2 * pad

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = pad
    for ln, b, h in zip(lines, dims, line_h):
        w = b[2] - b[0]
        d.text(((W - w) // 2 - b[0], y - b[1]), ln, font=font, fill=(255, 255, 255, 255))
        y += h + gap
    painted = float((np.asarray(img)[:, :, 3] > 10).mean())
    return img, painted


def _overlay_kml(name, lat, lon, half_len_m, half_wid_m, rotation, href):
    dlat = half_len_m / 111_320.0
    dlon = half_wid_m / (111_320.0 * math.cos(math.radians(lat)))
    return (
        f"  <GroundOverlay>\n"
        f"    <name>{temp2._xml_escape(name)}</name>\n"
        f"    <Icon><href>{href}</href></Icon>\n"
        f"    <LatLonBox>\n"
        f"      <north>{lat + dlat:.8f}</north><south>{lat - dlat:.8f}</south>\n"
        f"      <east>{lon + dlon:.8f}</east><west>{lon - dlon:.8f}</west>\n"
        f"      <rotation>{rotation:.2f}</rotation>\n"
        f"    </LatLonBox>\n"
        f"  </GroundOverlay>")


def build_text_kmz(points, text, offline=False, rate=None, zone=None,
                   keep_points=True, doc_name="Road Text Markings", log=print):
    """
    Return (kmz_bytes, boq_list). Each point gets the same text image draped on
    the road, oriented to the road bearing and sized to the carriageway.
    """
    if not (text or "").strip():
        raise ValueError("No text supplied to paint.")

    selected_zone = zone or temp2.config.DEFAULT_ZONE
    applied_rate = rate if rate is not None else temp2.config.get_zone_rate(selected_zone)

    img, painted_frac = render_text_image(text)
    aspect = img.width / img.height           # width / height of the rendered block
    img_buf = io.BytesIO(); img.save(img_buf, "PNG")
    img_bytes = img_buf.getvalue()
    href = "files/text.png"

    get_elements = temp2.make_elements_fetcher(points, offline, log)

    kml = ["<?xml version='1.0' encoding='UTF-8'?>",
           "<kml xmlns='http://www.opengis.net/kml/2.2'>",
           f"<Document><name>{temp2._xml_escape(doc_name)}</name>",
           f"  <Style id='pt'><IconStyle><scale>0.7</scale></IconStyle></Style>"]
    boq = []

    for idx, (lon, lat, name) in enumerate(points, 1):
        frame = temp2.LocalFrame(lat, lon)
        elements = get_elements(lat, lon)
        el, nodes_xy, dist = temp2.choose_road(frame, elements)

        warn = False
        if el is None:
            bearing = 0.0
            width = temp2.ROAD_WIDTH_DEFAULTS["_default"]
            hw_type, road_name, dist = "-", "-", float("nan")
            warn = True
        else:
            tags = el.get("tags", {})
            hw_type = tags.get("highway", "?")
            road_name = tags.get("name", "-")
            bearing = temp2.road_bearing_at(nodes_xy)
            width, _ = temp2.estimate_width(tags, name, idx)
            if dist > temp2.FAR_ROAD_WARN:
                warn = True

        # Ground footprint: block width across road, length along road (elongated).
        block_w = max(MIN_BLOCK_M, width * WIDTH_FRACTION)     # across road (E-W pre-rotation)
        block_len = block_w / aspect * TEXT_STRETCH            # along road (N-S pre-rotation)
        rotation = (-bearing) % 360                            # KML rotation is CCW from north

        kml.append(_overlay_kml(name or text, lat, lon,
                                block_len / 2, block_w / 2, rotation, href))

        area = painted_frac * block_w * block_len              # actual painted m2
        log(f"  [{idx}/{len(points)}] {name}: '{text}' on {hw_type} '{road_name}'  "
            f"bearing={bearing:.0f}deg  block={block_w:.1f}x{block_len:.1f}m  "
            f"paint={area:.1f}m2")
        boq.append({
            "S.No": idx, "Placemark": name, "Text": text, "Zone": selected_zone,
            "Lat": round(lat, 7), "Lon": round(lon, 7),
            "Road type": hw_type, "Road name": road_name,
            "Bearing (deg)": round(bearing, 1),
            "Block WxL (m)": f"{block_w:.1f} x {block_len:.1f}",
            "Painted area (m2)": round(area, 2),
            "Rate (Rs/m2)": applied_rate,
            "Amount (Rs)": round(area * applied_rate, 2),
            "Flag": "CHECK" if warn else "",
        })

    if keep_points:
        kml.append("  <Folder><name>Original points</name>")
        for lon, lat, name in points:
            kml.append(temp2.point_kml(name, lon, lat))
        kml.append("  </Folder>")

    kml += ["</Document>", "</kml>"]
    kml_str = "\n".join(kml)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", kml_str)
        z.writestr(href, img_bytes)
    return buf.getvalue(), boq
