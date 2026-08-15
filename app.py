"""
Road Marking Generator - web app
================================
Upload a KML of marked points and get back road-fitting markings:
  * Polygon mode - full-width paint strips that follow each road.
  * Text mode    - a word/phrase (e.g. "GO SLOW") painted along each road.

Output downloads as KML (polygon) or KMZ (text), with a BOQ spreadsheet and a
one-click "Open in Google Earth" (works when this app runs on your machine).

Run:  pip install flask
      python app.py
      open http://127.0.0.1:5000
"""
import io, os, uuid, tempfile, glob, subprocess, sys, shutil
from flask import Flask, request, render_template, send_file, jsonify, abort
import pandas as pd
import temp2updated as temp2, textmode

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

RESULTS = {}   # id -> dict(data=bytes, ext, mime, boq, name, columns)

KML_MIME = "application/vnd.google-earth.kml+xml"
KMZ_MIME = "application/vnd.google-earth.kmz"

POLY_COLS = ["S.No", "Placemark", "Road type", "Width (m)", "Mark length (m)", "Area (m2)", "Amount (Rs)", "Flag"]
TEXT_COLS = ["S.No", "Placemark", "Text", "Road type", "Painted area (m2)", "Amount (Rs)", "Flag"]


def _float(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    f = request.files.get("kml")
    if f is None or not f.filename:
        return jsonify(error="Please choose a .kml file."), 400
    if not f.filename.lower().endswith(".kml"):
        return jsonify(error="That doesn't look like a .kml file."), 400

    raw = f.read().decode("utf-8", errors="replace")
    try:
        points = temp2.parse_kml_string(raw)
    except ValueError as e:
        return jsonify(error=f"Could not read the KML: {e}"), 400
    if not points:
        return jsonify(error="No point placemarks were found in this KML."), 400

    mode = (request.form.get("mode") or "polygon").lower()
    rate = _float(request.form.get("rate"), temp2.RATE_PER_SQM)
    length = _float(request.form.get("length"), temp2.MARK_LENGTH)
    speed_breaker = request.form.get("speed_breaker", "").strip()
    strip_width = _float(request.form.get("speed_quantity"), 0.40)
    speed_gap = _float(request.form.get("speed_gap"), 0.60)
    speed_count = int(_float(request.form.get("speed_count"), 6 if speed_breaker else 1))
    road_text = request.form.get("road_text", "").strip()
    base = os.path.splitext(os.path.basename(f.filename))[0]

    try:
        if mode == "text":
            text = (request.form.get("text") or "").strip()
            if not text:
                return jsonify(error="Please enter or select the text to paint (e.g. GO SLOW)."), 400
            data, boq = textmode.build_text_kmz(
                points, text, rate=rate, log=lambda *_: None,
                doc_name=f"Road Text - {base}")
            ext, mime, cols = "kmz", KMZ_MIME, TEXT_COLS
            area_key, amount_key = "Painted area (m2)", "Amount (Rs)"
        else:
            kml_str, boq = temp2.build_polygons(
                points,
                mark_length=length,
                rate=rate,
                speed_breaker=speed_breaker,
                speed_quantity=strip_width,
                speed_gap=speed_gap,
                speed_count=speed_count,
                road_text=road_text,
                log=lambda *_: None,
                doc_name=f"Road Markings - {base}"
            )
            data, ext, mime, cols = kml_str.encode("utf-8"), "kml", KML_MIME, POLY_COLS
            area_key, amount_key = "Area (m2)", "Amount (Rs)"
    except Exception as e:
        return jsonify(error=f"Processing failed: {e}"), 500

    rid = uuid.uuid4().hex[:12]
    RESULTS[rid] = dict(data=data, ext=ext, mime=mime, boq=boq, name=base, columns=cols)

    # Strip _corners before sending to JSON (not serialisable), but keep them
    # in RESULTS so export_excel_boq can use them for satellite screenshots.
    boq_json = [{k: v for k, v in b.items() if k != "_corners"} for b in boq]

    return jsonify(
        id=rid, mode=mode, name=base, points=len(points),
        total_area=round(sum(b[area_key] for b in boq), 2),
        total_amount=round(sum(b[amount_key] for b in boq), 2),
        flagged=sum(1 for b in boq if b["Flag"]),
        columns=cols, boq=boq_json,
        download_url=f"/download/{rid}", boq_url=f"/boq/{rid}",
        download_name=f"{'text' if mode=='text' else 'out'}_{base}.{ext}",
    )


# ==================================================================
# IMAGE-MARKER WORKFLOW (placeholder codes -> image KMZ -> grouped BOQ)
# Step 1: /generate_kml  - upload placeholder KML, get a KMZ of image markers.
# Step 2: /generate_boq  - upload the client-finalised KML, get a grouped,
#                          per-patch Excel BOQ. (No Excel is made in step 1.)
# ==================================================================

def _read_kml_points(f):
    """Shared upload validation -> parsed points. Accepts .kml or .kmz (Google
    Earth exports finalised files as KMZ). Returns (points, error_response)."""
    if f is None or not f.filename:
        return None, (jsonify(error="Please choose a .kml or .kmz file."), 400)
    if not f.filename.lower().endswith((".kml", ".kmz")):
        return None, (jsonify(error="That doesn't look like a .kml or .kmz file."), 400)
    try:
        points = temp2.parse_kml_or_kmz_bytes(f.read())
    except ValueError as e:
        return None, (jsonify(error=f"Could not read the file: {e}"), 400)
    if not points:
        return None, (jsonify(error="No point placemarks were found in this file."), 400)
    return points, None


@app.route("/generate_kml", methods=["POST"])
def generate_kml():
    """Step 1: placeholder KML -> KMZ with the mapped image at each placemark."""
    points, err = _read_kml_points(request.files.get("kml"))
    if err:
        return err
    base = os.path.splitext(os.path.basename(request.files["kml"].filename))[0]

    try:
        data, summary = temp2.build_image_kmz(
            points, doc_name=f"Road Markings - {base}", log=lambda *_: None)
    except Exception as e:
        return jsonify(error=f"Generation failed: {e}"), 500

    rid = uuid.uuid4().hex[:12]
    RESULTS[rid] = dict(data=data, ext="kmz", mime=KMZ_MIME, boq=[], name=base, columns=[])

    unknown = [s["code"] or "(blank)" for s in summary if not s["known"]]
    return jsonify(
        id=rid, name=base, points=len(points),
        known=sum(1 for s in summary if s["known"]),
        unknown_codes=sorted(set(unknown)),
        download_url=f"/download/{rid}",
        download_name=f"markers_{base}.kmz",
    )


@app.route("/generate_boq", methods=["POST"])
def generate_boq():
    """Step 2: finalised KML -> grouped, per-patch Excel BOQ."""
    points, err = _read_kml_points(request.files.get("kml"))
    if err:
        return err
    base = os.path.splitext(os.path.basename(request.files["kml"].filename))[0]

    try:
        grouped = temp2.build_grouped_boq(points)
        buf = io.BytesIO()
        temp2.export_grouped_excel(grouped, buf)
        buf.seek(0)
    except Exception as e:
        return jsonify(error=f"BOQ generation failed: {e}"), 500

    rid = uuid.uuid4().hex[:12]
    RESULTS[rid] = dict(data=buf.getvalue(), ext="xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        boq=[], name=base, columns=[])

    # Per-patch preview for the UI.
    patches = [{
        "patch": patch,
        "items": rows,
        "total_qty": sum(r["Quantity"] for r in rows),
        "total_cost": round(sum(r["Total Cost"] for r in rows), 2),
    } for patch, rows in grouped.items()]

    return jsonify(
        id=rid, name=base, points=len(points), patches=patches,
        download_url=f"/download/{rid}",
        download_name=f"BOQ_{base}.xlsx",
    )


@app.route("/download/<rid>")
def download(rid):
    r = RESULTS.get(rid)
    if r is None:
        abort(404)
    return send_file(io.BytesIO(r["data"]), mimetype=r["mime"],
                     as_attachment=True, download_name=f"out_{r['name']}.{r['ext']}")


@app.route("/boq/<rid>")
def boq(rid):
    r = RESULTS.get(rid)
    if r is None:
        abort(404)
    buf = io.BytesIO()
    # Use export_excel_boq so satellite screenshots are embedded in the sheet.
    # Only polygon mode BOQ rows carry _corners; text mode falls back to plain.
    if r["boq"] and "_corners" in r["boq"][0]:
        temp2.export_excel_boq(r["boq"], buf)
    else:
        pd.DataFrame([{k: v for k, v in b.items() if k != "_corners"} for b in r["boq"]]).to_excel(buf, index=False)
    buf.seek(0)
    return send_file(
        buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name=f"BOQ_{r['name']}.xlsx")


def launch_in_google_earth_pro(filepath):
    """
    Attempts to launch a KML/KMZ file in Google Earth Pro (desktop app).
    Supports Windows native, WSL (Windows Subsystem for Linux), macOS, and Linux.
    Returns (success: bool, message/error: str)
    """
    filepath = os.path.abspath(filepath)
    if not os.path.exists(filepath):
        return False, f"File not found: {filepath}"

    # Detect WSL
    is_wsl = False
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/version", "r") as f:
                if "microsoft" in f.read().lower():
                    is_wsl = True
        except Exception:
            pass

    if is_wsl:
        # Convert WSL Linux path to Windows path
        win_filepath = filepath
        try:
            res = subprocess.run(["wslpath", "-w", filepath], capture_output=True, text=True)
            if res.returncode == 0 and res.stdout.strip():
                win_filepath = res.stdout.strip()
        except Exception:
            pass

        # Candidate Google Earth Pro executable paths in Windows (from WSL /mnt/c)
        wsl_ge_candidates = [
            "/mnt/c/Program Files/Google/Google Earth Pro/client/googleearth.exe",
            "/mnt/c/Program Files (x86)/Google/Google Earth Pro/client/googleearth.exe",
            "/mnt/c/Program Files/Google/Google Earth/client/googleearth.exe",
            "/mnt/c/Program Files (x86)/Google/Google Earth/client/googleearth.exe",
        ]
        try:
            user_appdata = glob.glob("/mnt/c/Users/*/AppData/Local/Google/Google Earth Pro/client/googleearth.exe")
            wsl_ge_candidates.extend(user_appdata)
        except Exception:
            pass

        # Try launching via explicit Google Earth Pro executable
        for ge_exe in wsl_ge_candidates:
            if os.path.exists(ge_exe):
                try:
                    subprocess.Popen([ge_exe, win_filepath])
                    return True, "Launched Google Earth Pro via WSL"
                except Exception:
                    pass

        # Fallback to cmd.exe /c start "" "win_filepath"
        try:
            subprocess.Popen(["cmd.exe", "/c", "start", "", win_filepath])
            return True, "Launched system default handler via cmd.exe"
        except Exception:
            pass

        # Fallback to powershell.exe
        try:
            subprocess.Popen(["powershell.exe", "-Command", f"Start-Process '{win_filepath}'"])
            return True, "Launched system default handler via powershell.exe"
        except Exception as e:
            return False, f"Failed to launch on WSL: {e}"

    # Native Windows
    if sys.platform == "win32":
        win_ge_candidates = [
            r"C:\Program Files\Google\Google Earth Pro\client\googleearth.exe",
            r"C:\Program Files (x86)\Google\Google Earth Pro\client\googleearth.exe",
            r"C:\Program Files\Google\Google Earth\client\googleearth.exe",
            r"C:\Program Files (x86)\Google\Google Earth\client\googleearth.exe",
        ]
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            win_ge_candidates.append(os.path.join(local_appdata, r"Google\Google Earth Pro\client\googleearth.exe"))
        
        program_files = os.environ.get("PROGRAMFILES", "")
        if program_files:
            win_ge_candidates.append(os.path.join(program_files, r"Google\Google Earth Pro\client\googleearth.exe"))

        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "")
        if program_files_x86:
            win_ge_candidates.append(os.path.join(program_files_x86, r"Google\Google Earth Pro\client\googleearth.exe"))

        for ge_exe in win_ge_candidates:
            if os.path.exists(ge_exe):
                try:
                    subprocess.Popen([ge_exe, filepath])
                    return True, "Launched Google Earth Pro directly"
                except Exception:
                    pass

        # Try os.startfile
        try:
            os.startfile(filepath)
            return True, "Launched via os.startfile"
        except Exception:
            pass

        # Try cmd.exe /c start ""
        try:
            subprocess.Popen(f'start "" "{filepath}"', shell=True)
            return True, "Launched via cmd.exe start"
        except Exception as e:
            return False, f"Failed to launch on Windows: {e}"

    # macOS
    if sys.platform == "darwin":
        try:
            subprocess.Popen(["open", "-a", "Google Earth Pro", filepath])
            return True, "Launched Google Earth Pro on macOS"
        except Exception:
            try:
                subprocess.Popen(["open", filepath])
                return True, "Launched default KML handler on macOS"
            except Exception as e:
                return False, f"Failed on macOS: {e}"

    # Linux (non-WSL)
    if shutil.which("google-earth-pro"):
        try:
            subprocess.Popen(["google-earth-pro", filepath])
            return True, "Launched google-earth-pro on Linux"
        except Exception:
            pass

    if shutil.which("xdg-open"):
        try:
            subprocess.Popen(["xdg-open", filepath])
            return True, "Launched xdg-open on Linux"
        except Exception as e:
            return False, f"Failed xdg-open on Linux: {e}"

    return False, "No desktop app launcher found"


@app.route("/open/<rid>", methods=["POST"])
def open_in_earth(rid):
    """Open the generated file in the machine's default KML handler
    (Google Earth Pro). Only works when the app runs on the user's own PC."""
    r = RESULTS.get(rid)
    if r is None:
        abort(404)

    ext = r.get("ext", "kmz")
    filename = f"roadmark_{rid}.{ext}"

    # Detect WSL to write to Windows Public Documents directory if available
    is_wsl = False
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/version", "r") as f:
                if "microsoft" in f.read().lower():
                    is_wsl = True
        except Exception:
            pass

    if is_wsl and os.path.exists("/mnt/c/Users/Public"):
        win_doc_dir = "/mnt/c/Users/Public/Documents"
        os.makedirs(win_doc_dir, exist_ok=True)
        path = os.path.join(win_doc_dir, filename)
    else:
        path = os.path.join(tempfile.gettempdir(), filename)

    with open(path, "wb") as fh:
        fh.write(r["data"])

    success, msg = launch_in_google_earth_pro(path)
    if success:
        return jsonify(ok=True, message=msg)
    else:
        return jsonify(ok=False, error=msg)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
