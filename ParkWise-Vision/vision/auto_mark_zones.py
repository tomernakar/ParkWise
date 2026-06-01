"""
ParkWise - Automatic Zone Marking Tool
======================================

Captures a live camera frame, auto-detects rectangular parking spaces using
contour detection, and lets you fix missed/wrong spots manually before saving.

By default it ALWAYS does a fresh auto-detection (reliable). Camera-drift
re-alignment via ORB is OPT-IN with --realign (it can be slow/fragile, so it
is never on the default path).

USAGE  (run from anywhere - file paths are anchored to the ParkWise-Vision folder)
-----
    python auto_mark_zones.py                     # external camera, auto-detect
    python auto_mark_zones.py --camera 1          # different camera index
    python auto_mark_zones.py --image baseline.jpg# static image, no camera
    python auto_mark_zones.py --sensitivity high  # low / medium / high
    python auto_mark_zones.py --realign           # opt-in: re-align saved zones

CAPTURE phase (camera only)
    SPACE      : capture the current frame
    Q / ESC    : abort

REVIEW phase (after detection)
    S / ENTER  : save zones + baseline and exit
    E          : enter manual EDIT mode
    R          : retry detection with the next sensitivity preset
    Q / ESC    : quit without saving

EDIT phase (manual fix)
    Click zone : select it (turns red)
    X / Del    : delete the selected zone
    Click empty: place a corner (4 clicks = 1 new zone)
    Right-click: undo last corner / delete selected / delete last
    D          : clear ALL zones
    A          : back to review
    S / ENTER  : save and exit
    Q / ESC    : quit without saving
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import cv2
import numpy as np


# --------------------------------------------------------------------------
# Unicode-safe image IO (cv2.imread/imwrite fail on non-ASCII / Hebrew paths)
# --------------------------------------------------------------------------
def imread_unicode(path, flags=cv2.IMREAD_COLOR):
    try:
        data = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite_unicode(path, img):
    ext = os.path.splitext(path)[1] or ".jpg"
    ok, buf = cv2.imencode(ext, img)
    if ok:
        buf.tofile(path)
    return ok


# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
WINDOW_NAME = "ParkWise - Auto Zone Marking"

# Always read/write data in the ParkWise-Vision folder (parent of this dir),
# regardless of the current working directory.
DATA_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZONES_FILE    = os.path.join(DATA_DIR, "zones.json")
BASELINE_FILE = os.path.join(DATA_DIR, "baseline.jpg")

# Colors (BGR)
COLOR_AUTO       = (0, 220, 100)    # green  - auto-detected
COLOR_MANUAL     = (0, 180, 255)    # orange - manually added
COLOR_ALIGNED    = (255, 200, 0)    # cyan   - re-aligned
COLOR_SELECTED   = (60, 60, 255)    # red    - selected for deletion
COLOR_PENDING_PT = (0, 165, 255)
COLOR_PENDING_LN = (0, 200, 255)
COLOR_FILL_ALPHA = 0.28
COLOR_STATUS_BG  = (30, 30, 30)
COLOR_STATUS_FG  = (230, 230, 230)
COLOR_HINT_FG    = (150, 220, 150)

FONT         = cv2.FONT_HERSHEY_SIMPLEX
HEADER_H     = 54
LABEL_SCALE  = 0.62
LABEL_THICK  = 2
POINT_RADIUS = 5
LINE_THICK   = 2
POINTS_PER_ZONE = 4

# Sensitivity presets: (min_area_ratio, max_area_ratio, approx_eps, asp_min, asp_max)
PRESETS = {
    "low":    (0.003, 0.06, 0.030, 1.1, 6.0),
    "medium": (0.001, 0.05, 0.025, 1.0, 7.0),
    "high":   (0.0005, 0.04, 0.020, 1.0, 8.0),
}
SENSITIVITY_CYCLE = ["low", "medium", "high"]


# --------------------------------------------------------------------------
# Camera capture
# --------------------------------------------------------------------------
def live_capture_frame(camera_index):
    cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print("Error: could not open camera index %d." % camera_index)
        return None

    print("Live preview: position the camera, then press SPACE to capture (Q to abort).")
    cv2.namedWindow(WINDOW_NAME)
    captured = None
    fail = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                fail += 1
                if fail > 60:
                    print("Error: camera not returning frames (is it in use?).")
                    break
                cv2.waitKey(10)
                continue
            fail = 0
            preview = frame.copy()
            h, w = preview.shape[:2]
            cv2.rectangle(preview, (0, 0), (w, 30), COLOR_STATUS_BG, -1)
            cv2.putText(preview, "LIVE - SPACE = capture   Q = abort",
                        (8, 21), FONT, 0.55, COLOR_STATUS_FG, 1, cv2.LINE_AA)
            cv2.imshow(WINDOW_NAME, preview)
            key = cv2.waitKey(1) & 0xFF
            if key == ord(' '):
                captured = frame.copy()
                print("Frame captured.")
                break
            if key in (ord('q'), 27):
                print("Aborted.")
                break
    finally:
        cap.release()
    return captured


# --------------------------------------------------------------------------
# Auto-detection
# --------------------------------------------------------------------------
def detect_parking_spaces(frame, sensitivity="medium"):
    """Detect individual parking spaces (white cells enclosed by black lines)."""
    min_r, max_r, eps, asp_min, asp_max = PRESETS[sensitivity]
    ih, iw = frame.shape[:2]
    img_area = ih * iw

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame.copy()
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(blurred, 255,
                                   cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 21, 4)
    binary = cv2.dilate(binary, np.ones((2, 2), np.uint8), iterations=1)

    contours, hierarchy = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None or len(contours) == 0:
        return []
    hier = hierarchy[0]

    raw = []
    for i, cnt in enumerate(contours):
        if hier[i][3] < 0:                       # keep only holes (child contours)
            continue
        area = cv2.contourArea(cnt)
        if not (img_area * min_r <= area <= img_area * max_r):
            continue
        approx = cv2.approxPolyDP(cnt, eps * cv2.arcLength(cnt, True), True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        x, y, w, h = cv2.boundingRect(approx)
        if min(w, h) == 0:
            continue
        if not (asp_min <= max(w, h) / min(w, h) <= asp_max):
            continue
        raw.append(approx.reshape(4, 2).tolist())

    # Deduplicate by centroid proximity
    deduped = []
    for pts in raw:
        cx = sum(p[0] for p in pts) / 4
        cy = sum(p[1] for p in pts) / 4
        if not any(abs(cx - sum(q[0] for q in z) / 4) < 8 and
                   abs(cy - sum(q[1] for q in z) / 4) < 8 for z in deduped):
            deduped.append(pts)
    if not deduped:
        return []

    # Order into rows robustly (handles slight tilt): cluster by Y gaps, sort X
    heights = [max(p[1] for p in pts) - min(p[1] for p in pts) for pts in deduped]
    median_h = sorted(heights)[len(heights) // 2] if heights else 30
    row_gap = max(median_h * 0.6, 15)

    by_y = sorted(deduped, key=lambda pts: sum(p[1] for p in pts) / 4)
    rows, current, prev_cy = [], [by_y[0]], sum(p[1] for p in by_y[0]) / 4
    for pts in by_y[1:]:
        cy = sum(p[1] for p in pts) / 4
        if cy - prev_cy > row_gap:
            rows.append(current)
            current = []
        current.append(pts)
        prev_cy = cy
    rows.append(current)

    ordered = []
    for row in rows:
        row.sort(key=lambda pts: sum(p[0] for p in pts) / 4)
        ordered.extend(row)

    return [{"id": "S%d" % (i + 1),
             "polygon": [[int(x), int(y)] for x, y in pts],
             "source": "auto"}
            for i, pts in enumerate(ordered)]


# --------------------------------------------------------------------------
# ORB re-alignment (opt-in via --realign)
# --------------------------------------------------------------------------
def align_zones_to_frame(baseline_img, current_frame, zones):
    def gray(img):
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img

    base = baseline_img
    cur = current_frame
    # Match sizes so ORB/homography behave predictably
    if base.shape[:2] != cur.shape[:2]:
        cur = cv2.resize(cur, (base.shape[1], base.shape[0]))

    orb = cv2.ORB_create(nfeatures=1500)
    kp1, des1 = orb.detectAndCompute(gray(base), None)
    kp2, des2 = orb.detectAndCompute(gray(cur), None)
    if des1 is None or des2 is None or len(kp1) < 8 or len(kp2) < 8:
        print("Warning: not enough features - using saved zones as-is.")
        return None, zones

    matches = sorted(cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(des1, des2),
                     key=lambda m: m.distance)
    if len(matches) < 10:
        print("Warning: only %d matches - alignment skipped." % len(matches))
        return None, zones

    top = matches[:min(80, len(matches))]
    src = np.float32([kp1[m.queryIdx].pt for m in top]).reshape(-1, 1, 2)
    dst = np.float32([kp2[m.trainIdx].pt for m in top]).reshape(-1, 1, 2)
    H, _ = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    if H is None:
        print("Warning: homography failed - using saved zones as-is.")
        return None, zones

    aligned = []
    for z in zones:
        pts = np.float32(z["polygon"]).reshape(-1, 1, 2)
        new = cv2.perspectiveTransform(pts, H).reshape(-1, 2)
        aligned.append({"id": z["id"],
                        "polygon": [[int(round(x)), int(round(y))] for x, y in new],
                        "source": "aligned"})
    print("Alignment OK.")
    return H, aligned


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------
def _centroid(poly):
    return (int(sum(p[0] for p in poly) / len(poly)),
            int(sum(p[1] for p in poly) / len(poly)))


def _label(img, text, center):
    (tw, th), _ = cv2.getTextSize(text, FONT, LABEL_SCALE, LABEL_THICK)
    org = (center[0] - tw // 2, center[1] + th // 2)
    cv2.putText(img, text, org, FONT, LABEL_SCALE, (0, 0, 0), LABEL_THICK + 2, cv2.LINE_AA)
    cv2.putText(img, text, org, FONT, LABEL_SCALE, (255, 255, 255), LABEL_THICK, cv2.LINE_AA)


def _header(canvas, line1, line2, bg=COLOR_STATUS_BG):
    w = canvas.shape[1]
    cv2.rectangle(canvas, (0, 0), (w, HEADER_H), bg, -1)
    cv2.line(canvas, (0, HEADER_H), (w, HEADER_H), (90, 90, 90), 1)
    cv2.putText(canvas, line1, (8, 21), FONT, 0.52, COLOR_STATUS_FG, 1, cv2.LINE_AA)
    cv2.putText(canvas, line2, (8, 44), FONT, 0.44, COLOR_HINT_FG, 1, cv2.LINE_AA)


def draw_review(base, zones, sensitivity, mode_label):
    canvas = base.copy()
    if zones:
        fill = canvas.copy()
        for z in zones:
            pts = np.array(z["polygon"], dtype=np.int32)
            col = COLOR_ALIGNED if z.get("source") == "aligned" else COLOR_AUTO
            cv2.fillPoly(fill, [pts], col)
        cv2.addWeighted(fill, COLOR_FILL_ALPHA, canvas, 1 - COLOR_FILL_ALPHA, 0, canvas)
        for z in zones:
            pts = np.array(z["polygon"], dtype=np.int32)
            col = COLOR_ALIGNED if z.get("source") == "aligned" else COLOR_AUTO
            cv2.polylines(canvas, [pts], True, col, 2, cv2.LINE_AA)
            _label(canvas, z["id"], _centroid(z["polygon"]))
        _header(canvas,
                "%s  -  %d zones  [sens=%s]" % (mode_label, len(zones), sensitivity),
                "S=save   E=edit manually   R=change sensitivity   Q=quit")
    else:
        _header(canvas,
                "NO ZONES DETECTED  [sens=%s]" % sensitivity,
                "R=try next sensitivity   E=edit manually   Q=quit",
                bg=(0, 40, 100))
    return canvas


def draw_edit(base, st):
    canvas = base.copy()
    zones, sel = st["zones"], st.get("selected")
    if zones:
        fill = canvas.copy()
        for z in zones:
            pts = np.array(z["polygon"], dtype=np.int32)
            col = COLOR_MANUAL if z.get("source") == "manual" else COLOR_AUTO
            cv2.fillPoly(fill, [pts], col)
        cv2.addWeighted(fill, COLOR_FILL_ALPHA, canvas, 1 - COLOR_FILL_ALPHA, 0, canvas)
        for i, z in enumerate(zones):
            pts = np.array(z["polygon"], dtype=np.int32)
            if i == sel:
                cv2.fillPoly(canvas, [pts], COLOR_SELECTED)
                cv2.polylines(canvas, [pts], True, COLOR_SELECTED, 3, cv2.LINE_AA)
            else:
                col = COLOR_MANUAL if z.get("source") == "manual" else COLOR_AUTO
                cv2.polylines(canvas, [pts], True, col, 2, cv2.LINE_AA)
            _label(canvas, z["id"], _centroid(z["polygon"]))

    for i, p in enumerate(st["pending"]):
        cv2.circle(canvas, tuple(p), POINT_RADIUS, COLOR_PENDING_PT, -1, cv2.LINE_AA)
        if i > 0:
            cv2.line(canvas, tuple(st["pending"][i - 1]), tuple(p),
                     COLOR_PENDING_LN, LINE_THICK, cv2.LINE_AA)

    n_auto = sum(1 for z in zones if z.get("source") != "manual")
    n_man = sum(1 for z in zones if z.get("source") == "manual")
    if st["pending"]:
        hint = "drawing: click %d more corner(s)" % (POINTS_PER_ZONE - len(st["pending"]))
    elif sel is not None:
        hint = "%s selected" % zones[sel]["id"]
    else:
        hint = "click a zone to select / empty space to add"
    _header(canvas,
            "EDIT MODE  -  auto:%d manual:%d  (%s)" % (n_auto, n_man, hint),
            "Click=select/add  X=delete  R-click=undo/del  D=clear  A=back  S=save  Q=quit")
    return canvas


# --------------------------------------------------------------------------
# Edit helpers + mouse callback
# --------------------------------------------------------------------------
def zone_at_point(zones, x, y):
    for i, z in enumerate(zones):
        if cv2.pointPolygonTest(np.array(z["polygon"], dtype=np.int32),
                                (float(x), float(y)), False) >= 0:
            return i
    return None


def renumber(st):
    for i, z in enumerate(st["zones"]):
        z["id"] = "S%d" % (i + 1)
    st["next_index"] = len(st["zones"]) + 1


def delete_zone(st, idx):
    removed = st["zones"].pop(idx)
    st["selected"] = None
    renumber(st)
    print("Deleted zone %s" % removed["id"])


def mouse_callback(event, x, y, flags, st):
    if event == cv2.EVENT_LBUTTONDOWN:
        if not st["pending"]:
            hit = zone_at_point(st["zones"], x, y)
            if hit is not None:
                st["selected"] = None if st.get("selected") == hit else hit
                return
        st["selected"] = None
        st["pending"].append([x, y])
        if len(st["pending"]) == POINTS_PER_ZONE:
            st["zones"].append({"id": "S%d" % st["next_index"],
                                "polygon": st["pending"], "source": "manual"})
            st["pending"] = []
            renumber(st)
            print("Added new zone %s (manual)" % st["zones"][-1]["id"])
    elif event == cv2.EVENT_RBUTTONDOWN:
        if st["pending"]:
            st["pending"].pop()
        elif st.get("selected") is not None:
            delete_zone(st, st["selected"])
        elif st["zones"]:
            delete_zone(st, len(st["zones"]) - 1)


# --------------------------------------------------------------------------
# Save / load
# --------------------------------------------------------------------------
def save_zones(zones, image, lot_id):
    h, w = image.shape[:2]
    clean = [{"id": z["id"], "polygon": z["polygon"]} for z in zones]
    payload = {
        "lot_id": lot_id,
        "image_size": {"width": int(w), "height": int(h)},
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zones": clean,
    }
    with open(ZONES_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    imwrite_unicode(BASELINE_FILE, image)
    print("Saved %d zones -> %s" % (len(clean), ZONES_FILE))
    print("Saved baseline    -> %s" % BASELINE_FILE)


def load_existing(default_lot):
    try:
        with open(ZONES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        zones = [{"id": z["id"],
                  "polygon": [list(map(int, p)) for p in z["polygon"]],
                  "source": "saved"}
                 for z in data.get("zones", [])]
        return data.get("lot_id", default_lot), zones
    except (OSError, json.JSONDecodeError):
        return default_lot, []


# --------------------------------------------------------------------------
# Args + main
# --------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="ParkWise auto zone marking tool")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--camera", type=int, default=0,
                     help="Webcam index (default 0 - external camera)")
    src.add_argument("--image", type=str, help="Static image instead of camera")
    p.add_argument("--lot-id", type=str, default="demo_lot_1")
    p.add_argument("--sensitivity", type=str, default="medium",
                   choices=["low", "medium", "high"])
    p.add_argument("--realign", action="store_true",
                   help="Opt-in: ORB re-align saved zones to the current frame")
    p.add_argument("--overwrite", action="store_true",
                   help="(kept for compatibility; default already re-detects)")
    return p.parse_args()


def main():
    args = parse_args()
    lot_id, sens = args.lot_id, args.sensitivity

    # Acquire frame
    if args.image:
        frame = imread_unicode(args.image)
        if frame is None:
            sys.exit("Error: cannot read image '%s'" % args.image)
        print("Loaded image: %s" % args.image)
    else:
        frame = live_capture_frame(args.camera)
        if frame is None:
            sys.exit("Error: no frame captured.")

    cv2.namedWindow(WINDOW_NAME)

    # Re-alignment is OPT-IN (default path is always fresh detection -> never hangs)
    if args.realign and os.path.exists(ZONES_FILE) and os.path.exists(BASELINE_FILE):
        print("Re-alignment mode (--realign).")
        saved_lot, saved_zones = load_existing(lot_id)
        lot_id = saved_lot
        baseline = imread_unicode(BASELINE_FILE)
        if baseline is not None and saved_zones:
            H, zones = align_zones_to_frame(baseline, frame, saved_zones)
            label = "RE-ALIGNED" if H is not None else "SAVED (no drift)"
        else:
            zones, label = saved_zones, "SAVED"
    else:
        print("Auto-detecting zones (sensitivity=%s)..." % sens)
        zones = detect_parking_spaces(frame, sens)
        print("Detected %d zones." % len(zones))
        label = "AUTO-DETECTED"

    st = {"zones": zones, "pending": [], "next_index": len(zones) + 1, "selected": None}
    mode = "review"

    while True:
        if mode == "review":
            cv2.imshow(WINDOW_NAME, draw_review(frame, st["zones"], sens, label))
            key = cv2.waitKey(0) & 0xFF
            if key in (ord('s'), 13) and st["zones"]:
                save_zones(st["zones"], frame, lot_id)
                break
            elif key == ord('e'):
                mode = "edit"
                st["selected"], st["pending"] = None, []
                cv2.setMouseCallback(WINDOW_NAME, mouse_callback, st)
                print("Entering manual edit mode.")
            elif key == ord('r'):
                sens = SENSITIVITY_CYCLE[(SENSITIVITY_CYCLE.index(sens) + 1) % 3]
                print("Retrying with sensitivity=%s ..." % sens)
                zones = detect_parking_spaces(frame, sens)
                st = {"zones": zones, "pending": [], "next_index": len(zones) + 1, "selected": None}
                label = "AUTO-DETECTED"
                print("Detected %d zones." % len(zones))
            elif key in (ord('q'), 27):
                print("Exited without saving.")
                break

        elif mode == "edit":
            cv2.imshow(WINDOW_NAME, draw_edit(frame, st))
            key = cv2.waitKey(20) & 0xFF
            if key in (ord('s'), 13) and st["zones"]:
                save_zones(st["zones"], frame, lot_id)
                break
            elif key in (ord('x'), 127, 8):
                if st.get("selected") is not None:
                    delete_zone(st, st["selected"])
                else:
                    print("No zone selected. Click a zone first.")
            elif key == ord('d'):
                st["zones"].clear(); st["pending"].clear()
                st["selected"], st["next_index"] = None, 1
                print("Cleared all zones.")
            elif key == ord('a'):
                mode = "review"
                st["selected"], st["pending"] = None, []
            elif key in (ord('q'), 27):
                ans = input("Quit without saving? %d zone(s) lost. (y/N): "
                            % len(st["zones"])).strip().lower()
                if ans == "y":
                    print("Exited without saving.")
                    break

    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
