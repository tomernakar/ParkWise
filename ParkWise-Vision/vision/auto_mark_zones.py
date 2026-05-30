"""
ParkWise - Automatic Zone Marking Tool
=======================================

Captures a live frame, auto-detects rectangular parking zones using contour
detection, then lets you fix any missed spots manually before saving.

On first run  : captures frame -> auto-detects zones -> optional manual edit -> save
On re-runs    : captures frame -> ORB homography re-alignment of saved zones -> confirm

USAGE
-----
    python auto_mark_zones.py                        # camera 1 (Logitech), auto-detect
    python auto_mark_zones.py --camera 0             # laptop built-in camera
    python auto_mark_zones.py --image baseline.jpg   # static image (no camera)
    python auto_mark_zones.py --lot-id campus        # custom lot ID
    python auto_mark_zones.py --sensitivity high     # low / medium / high
    python auto_mark_zones.py --overwrite            # force re-detect, skip alignment

REVIEW mode (after auto-detection)
------------------------------------
    S / ENTER  : save all zones and exit
    E          : enter manual EDIT mode to fix missed / wrong zones
    R          : retry auto-detection with next sensitivity preset
    Q / ESC    : quit without saving

EDIT mode (manual fix)
-----------------------
    Left click  : add a corner point (every 4 clicks = 1 new zone)
    Right click : undo last point, or remove last zone if no pending points
    D           : delete ALL auto-detected zones (start clean)
    S / ENTER   : save all zones (auto-detected + manually added) and exit
    Q / ESC     : quit without saving
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WINDOW_NAME   = "ParkWise - Auto Zone Marking"
ZONES_FILE    = "zones.json"
BASELINE_FILE = "baseline.jpg"

# Colors (BGR)
COLOR_AUTO        = (0, 220, 100)    # green  - auto-detected
COLOR_MANUAL      = (0, 180, 255)    # orange - manually added
COLOR_ALIGNED     = (255, 200, 0)    # cyan   - re-aligned
COLOR_PENDING_PT  = (0, 165, 255)    # orange dot
COLOR_PENDING_LN  = (0, 200, 255)    # orange line
COLOR_SELECTED    = (60, 60, 255)    # red    - selected zone (for deletion)
COLOR_FILL_ALPHA  = 0.28
COLOR_STATUS_BG   = (30, 30, 30)
COLOR_STATUS_FG   = (230, 230, 230)
COLOR_WARN_FG     = (60, 120, 255)

FONT              = cv2.FONT_HERSHEY_SIMPLEX
STATUS_BAR_HEIGHT = 36
STATUS_SCALE      = 0.52
STATUS_THICK      = 1
LABEL_SCALE       = 0.62
LABEL_THICK       = 2
POINT_RADIUS      = 5
LINE_THICK        = 2

POINTS_PER_ZONE = 4

# Sensitivity presets (min_area_ratio, max_area_ratio, approx_eps, asp_min, asp_max)
# These target individual parking spaces (small white cells inside the grid)
PRESETS = {
    "low":    (0.003, 0.06, 0.03, 1.1, 6.0),
    "medium": (0.001, 0.05, 0.025, 1.0, 7.0),
    "high":   (0.0005, 0.04, 0.02, 1.0, 8.0),
}
SENSITIVITY_CYCLE = ["low", "medium", "high"]


# ---------------------------------------------------------------------------
# Camera capture  (identical to mark_zones.py)
# ---------------------------------------------------------------------------
def live_capture_frame(camera_index):
    cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print(f"Error: could not open camera index {camera_index}.")
        return None

    print("Live preview: position the camera, then press SPACE to capture (Q to abort).")
    cv2.namedWindow(WINDOW_NAME)
    captured = None
    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                print("Error: failed to read frame from camera.")
                break
            preview = frame.copy()
            h, w = preview.shape[:2]
            cv2.rectangle(preview, (0, 0), (w, STATUS_BAR_HEIGHT), COLOR_STATUS_BG, -1)
            cv2.putText(preview,
                        "LIVE - SPACE = capture frame   Q = abort",
                        (8, 23), FONT, STATUS_SCALE, COLOR_STATUS_FG, STATUS_THICK, cv2.LINE_AA)
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


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------
def detect_parking_spaces(frame, sensitivity="medium"):
    """
    Detect individual parking spaces from a top-down B&W grid image.

    Strategy: parking spaces are WHITE rectangles enclosed by BLACK lines.
    After adaptive thresholding + inversion, the black lines become a white
    connected network. The individual spaces are the HOLES in that network.
    RETR_CCOMP exposes those holes as child contours (parent index >= 0).
    """
    min_r, max_r, eps, asp_min, asp_max = PRESETS[sensitivity]
    ih, iw = frame.shape[:2]
    img_area = ih * iw

    gray    = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame.copy()
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)

    # Adaptive threshold: robust to uneven lighting from a camera photo
    binary = cv2.adaptiveThreshold(
        blurred, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        blockSize=21, C=4
    )

    # Slightly thicken lines so adjacent spaces stay separated
    k2 = np.ones((2, 2), np.uint8)
    binary = cv2.dilate(binary, k2, iterations=1)

    # RETR_CCOMP: outer contours at level 0, holes at level 1.
    # Individual parking spaces = holes -> parent index >= 0.
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None or len(contours) == 0:
        return []

    hier = hierarchy[0]   # shape (N, 4): [next, prev, first_child, parent]

    raw = []
    for i, cnt in enumerate(contours):
        # Only keep holes (child contours = individual spaces)
        if hier[i][3] < 0:
            continue

        area = cv2.contourArea(cnt)
        if not (img_area * min_r <= area <= img_area * max_r):
            continue

        peri   = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, eps * peri, True)

        if len(approx) != 4:
            continue
        if not cv2.isContourConvex(approx):
            continue

        x, y, w, h = cv2.boundingRect(approx)
        if min(w, h) == 0:
            continue
        aspect = max(w, h) / min(w, h)
        if not (asp_min <= aspect <= asp_max):
            continue

        raw.append(approx.reshape(4, 2).tolist())

    # Deduplicate: drop zones whose centroid is within 8px of an already-kept one
    deduped = []
    for pts in raw:
        cx = sum(p[0] for p in pts) / 4
        cy = sum(p[1] for p in pts) / 4
        if not any(abs(cx - sum(p[0] for p in z) / 4) < 8 and
                   abs(cy - sum(p[1] for p in z) / 4) < 8
                   for z in deduped):
            deduped.append(pts)

    if not deduped:
        return []

    # -- Order zones into rows robustly (handles slight paper/camera tilt) --
    # 1. Estimate a typical zone height from all detections.
    heights = []
    for pts in deduped:
        ys = [p[1] for p in pts]
        heights.append(max(ys) - min(ys))
    median_h = sorted(heights)[len(heights) // 2] if heights else 30
    row_gap = max(median_h * 0.6, 15)   # vertical jump that starts a new row

    # 2. Sort by Y centroid, then split into rows wherever the gap is large.
    by_y = sorted(deduped, key=lambda pts: sum(p[1] for p in pts) / 4)
    rows = []
    current_row = [by_y[0]]
    prev_cy = sum(p[1] for p in by_y[0]) / 4
    for pts in by_y[1:]:
        cy = sum(p[1] for p in pts) / 4
        if cy - prev_cy > row_gap:
            rows.append(current_row)
            current_row = []
        current_row.append(pts)
        prev_cy = cy
    rows.append(current_row)

    # 3. Within each row, sort left-to-right by X centroid.
    ordered = []
    for row in rows:
        row.sort(key=lambda pts: sum(p[0] for p in pts) / 4)
        ordered.extend(row)

    return [{"id": f"S{i+1}", "polygon": [[int(x), int(y)] for x, y in pts],
             "source": "auto"}
            for i, pts in enumerate(ordered)]


# ---------------------------------------------------------------------------
# ORB re-alignment
# ---------------------------------------------------------------------------
def align_zones_to_frame(baseline_img, current_frame, zones):
    def gray(img):
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img

    orb = cv2.ORB_create(nfeatures=2000)
    kp1, des1 = orb.detectAndCompute(gray(baseline_img),  None)
    kp2, des2 = orb.detectAndCompute(gray(current_frame), None)

    if des1 is None or des2 is None or len(kp1) < 8 or len(kp2) < 8:
        print("Warning: not enough features for alignment - using saved zones as-is.")
        return None, zones

    matches = sorted(cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(des1, des2),
                     key=lambda m: m.distance)
    if len(matches) < 10:
        print(f"Warning: only {len(matches)} matches - alignment skipped.")
        return None, zones

    top = matches[:min(80, len(matches))]
    src = np.float32([kp1[m.queryIdx].pt for m in top]).reshape(-1, 1, 2)
    dst = np.float32([kp2[m.trainIdx].pt for m in top]).reshape(-1, 1, 2)
    H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    if H is None:
        print("Warning: homography failed - using saved zones as-is.")
        return None, zones

    print(f"Alignment OK - {int(mask.sum()) if mask is not None else '?'} inliers used.")
    aligned = []
    for z in zones:
        pts     = np.float32(z["polygon"]).reshape(-1, 1, 2)
        new_pts = cv2.perspectiveTransform(pts, H).reshape(-1, 2)
        aligned.append({"id": z["id"],
                        "polygon": [[int(round(x)), int(round(y))] for x, y in new_pts],
                        "source": "aligned"})
    return H, aligned


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def _centroid(polygon):
    return (int(sum(p[0] for p in polygon) / len(polygon)),
            int(sum(p[1] for p in polygon) / len(polygon)))


def _draw_label(img, text, center):
    (tw, th), _ = cv2.getTextSize(text, FONT, LABEL_SCALE, LABEL_THICK)
    org = (center[0] - tw // 2, center[1] + th // 2)
    cv2.putText(img, text, org, FONT, LABEL_SCALE, (0,0,0),   LABEL_THICK+2, cv2.LINE_AA)
    cv2.putText(img, text, org, FONT, LABEL_SCALE, (255,255,255), LABEL_THICK, cv2.LINE_AA)


def _draw_header(canvas, line1, line2, bg=COLOR_STATUS_BG):
    """Draw a two-row header bar: status line + keyboard shortcuts line."""
    h, w = canvas.shape[:2]
    bar_h = 54
    cv2.rectangle(canvas, (0, 0), (w, bar_h), bg, -1)
    cv2.line(canvas, (0, bar_h), (w, bar_h), (90, 90, 90), 1)
    cv2.putText(canvas, line1, (8, 21), FONT, 0.52,
                COLOR_STATUS_FG, 1, cv2.LINE_AA)
    cv2.putText(canvas, line2, (8, 44), FONT, 0.44,
                (150, 220, 150), 1, cv2.LINE_AA)


def draw_review(base, zones, sensitivity, mode_label="AUTO-DETECTED"):
    canvas = base.copy()
    if zones:
        fill = canvas.copy()
        for z in zones:
            pts   = np.array(z["polygon"], dtype=np.int32)
            color = COLOR_ALIGNED if z.get("source") == "aligned" else COLOR_AUTO
            cv2.fillPoly(fill, [pts], color)
        cv2.addWeighted(fill, COLOR_FILL_ALPHA, canvas, 1 - COLOR_FILL_ALPHA, 0, canvas)
        for z in zones:
            pts   = np.array(z["polygon"], dtype=np.int32)
            color = COLOR_ALIGNED if z.get("source") == "aligned" else COLOR_AUTO
            cv2.polylines(canvas, [pts], True, color, 2, cv2.LINE_AA)
            _draw_label(canvas, z["id"], _centroid(z["polygon"]))

    if zones:
        line1 = "%s  -  %d zones  [sens=%s]" % (mode_label, len(zones), sensitivity)
        line2 = "S=save   E=edit manually   R=change sensitivity   Q=quit"
        _draw_header(canvas, line1, line2)
    else:
        line1 = "NO ZONES DETECTED  [sens=%s]" % sensitivity
        line2 = "R=try next sensitivity   E=edit manually   Q=quit"
        _draw_header(canvas, line1, line2, bg=(0, 40, 100))
    return canvas


def draw_edit(base, edit_state):
    canvas = base.copy()
    all_zones = edit_state["zones"]
    selected  = edit_state.get("selected")

    if all_zones:
        fill = canvas.copy()
        for z in all_zones:
            pts   = np.array(z["polygon"], dtype=np.int32)
            color = COLOR_MANUAL if z.get("source") == "manual" else COLOR_AUTO
            cv2.fillPoly(fill, [pts], color)
        cv2.addWeighted(fill, COLOR_FILL_ALPHA, canvas, 1 - COLOR_FILL_ALPHA, 0, canvas)
        for i, z in enumerate(all_zones):
            pts = np.array(z["polygon"], dtype=np.int32)
            if i == selected:
                cv2.fillPoly(canvas, [pts], COLOR_SELECTED)
                cv2.polylines(canvas, [pts], True, COLOR_SELECTED, 3, cv2.LINE_AA)
            else:
                color = COLOR_MANUAL if z.get("source") == "manual" else COLOR_AUTO
                cv2.polylines(canvas, [pts], True, color, 2, cv2.LINE_AA)
            _draw_label(canvas, z["id"], _centroid(z["polygon"]))

    pending = edit_state["pending"]
    for i, p in enumerate(pending):
        cv2.circle(canvas, tuple(p), POINT_RADIUS, COLOR_PENDING_PT, -1, cv2.LINE_AA)
        if i > 0:
            cv2.line(canvas, tuple(pending[i-1]), tuple(p), COLOR_PENDING_LN, LINE_THICK, cv2.LINE_AA)

    n_auto   = sum(1 for z in all_zones if z.get("source") != "manual")
    n_manual = sum(1 for z in all_zones if z.get("source") == "manual")
    np_ = len(pending)
    if np_ > 0:
        hint = "drawing: click %d more corner(s)" % (POINTS_PER_ZONE - np_)
    elif selected is not None:
        hint = "%s selected" % all_zones[selected]["id"]
    else:
        hint = "click a zone to select / empty space to add"

    line1 = "EDIT MODE  -  auto:%d manual:%d  (%s)" % (n_auto, n_manual, hint)
    line2 = ("Click=select/add  X=delete  R-click=undo/delete  "
             "D=clear all  A=back  S=save  Q=quit")
    _draw_header(canvas, line1, line2)
    return canvas


def zone_at_point(zones, x, y):
    """Return index of the zone whose polygon contains (x, y), or None."""
    for i, z in enumerate(zones):
        poly = np.array(z["polygon"], dtype=np.int32)
        if cv2.pointPolygonTest(poly, (float(x), float(y)), False) >= 0:
            return i
    return None


def renumber_zones(edit_state):
    """Reassign sequential S1..SN IDs in current list order (no gaps)."""
    for i, z in enumerate(edit_state["zones"]):
        z["id"] = f"S{i + 1}"
    edit_state["next_index"] = len(edit_state["zones"]) + 1


def delete_zone(edit_state, idx):
    removed = edit_state["zones"].pop(idx)
    edit_state["selected"] = None
    renumber_zones(edit_state)
    print(f"Deleted zone {removed['id']}")


# ---------------------------------------------------------------------------
# Mouse callback for manual edit
# ---------------------------------------------------------------------------
def mouse_callback(event, x, y, flags, edit_state):
    if event == cv2.EVENT_LBUTTONDOWN:
        # If not mid-drawing, a click inside an existing zone selects it.
        if not edit_state["pending"]:
            hit = zone_at_point(edit_state["zones"], x, y)
            if hit is not None:
                # Toggle selection if clicking the already-selected zone
                edit_state["selected"] = None if edit_state.get("selected") == hit else hit
                if edit_state["selected"] is not None:
                    print(f"Selected {edit_state['zones'][hit]['id']}")
                return

        # Otherwise add a corner point for a new zone
        edit_state["selected"] = None
        edit_state["pending"].append([x, y])
        if len(edit_state["pending"]) == POINTS_PER_ZONE:
            edit_state["zones"].append({
                "id":      f"S{edit_state['next_index']}",
                "polygon": edit_state["pending"],
                "source":  "manual",
            })
            edit_state["pending"] = []
            renumber_zones(edit_state)
            print(f"Added new zone {edit_state['zones'][-1]['id']} (manual)")

    elif event == cv2.EVENT_RBUTTONDOWN:
        if edit_state["pending"]:
            edit_state["pending"].pop()
        elif edit_state.get("selected") is not None:
            delete_zone(edit_state, edit_state["selected"])
        elif edit_state["zones"]:
            delete_zone(edit_state, len(edit_state["zones"]) - 1)


# ---------------------------------------------------------------------------
# Save / load
# ---------------------------------------------------------------------------
def save_zones(zones, image, lot_id):
    h, w = image.shape[:2]
    # Strip internal "source" key before writing
    clean = [{"id": z["id"], "polygon": z["polygon"]} for z in zones]
    payload = {
        "lot_id":     lot_id,
        "image_size": {"width": int(w), "height": int(h)},
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zones":      clean,
    }
    with open(ZONES_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    cv2.imwrite(BASELINE_FILE, image)
    print(f"Saved {len(clean)} zones -> {ZONES_FILE}")
    print(f"Saved baseline       -> {BASELINE_FILE}")


def load_existing(lot_id_default):
    try:
        with open(ZONES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        zones = [{"id": z["id"], "polygon": [list(map(int, p)) for p in z["polygon"]],
                  "source": "saved"}
                 for z in data.get("zones", [])]
        return data.get("lot_id", lot_id_default), zones
    except (OSError, json.JSONDecodeError):
        return lot_id_default, []


# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="ParkWise auto zone marking tool")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--camera",      type=int, default=0,
                     help="Webcam index (default 0 - external camera on this machine)")
    src.add_argument("--image",       type=str,
                     help="Path to a static image instead of camera")
    p.add_argument("--lot-id",        type=str, default="demo_lot_1")
    p.add_argument("--sensitivity",   type=str, default="medium",
                   choices=["low", "medium", "high"])
    p.add_argument("--overwrite",     action="store_true",
                   help="Force re-detection even if zones.json exists")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args   = parse_args()
    lot_id = args.lot_id
    sens   = args.sensitivity

    # -- Acquire base frame --
    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            sys.exit("Error: cannot read '%s'" % args.image)
        print("Loaded image: %s" % args.image)
    else:
        frame = live_capture_frame(args.camera)
        if frame is None:
            sys.exit("Error: no frame captured.")

    cv2.namedWindow(WINDOW_NAME)

    # -- Re-alignment mode (zones.json + baseline.jpg already exist) --
    if (not args.overwrite
            and os.path.exists(ZONES_FILE)
            and os.path.exists(BASELINE_FILE)):
        print("Found existing zones.json + baseline.jpg - running re-alignment.")
        saved_lot, saved_zones = load_existing(lot_id)
        lot_id   = saved_lot
        baseline = cv2.imread(BASELINE_FILE)
        if baseline is not None and saved_zones:
            H, zones = align_zones_to_frame(baseline, frame, saved_zones)
        else:
            H, zones = None, saved_zones
        label = "RE-ALIGNED" if H is not None else "SAVED (no drift)"
        mode  = "review"
    else:
        # -- Auto-detection mode --
        print("Auto-detecting zones (sensitivity=%s)..." % sens)
        zones = detect_parking_spaces(frame, sens)
        print("Detected %d zones." % len(zones))
        label = "AUTO-DETECTED"
        mode  = "review"

    # -- Event loop --
    edit_state = {
        "zones":      zones,
        "pending":    [],
        "next_index": len(zones) + 1,
        "selected":   None,
    }

    while True:

        # -- REVIEW mode --
        if mode == "review":
            cv2.imshow(WINDOW_NAME, draw_review(frame, edit_state["zones"], sens, label))
            key = cv2.waitKey(0) & 0xFF

            if key in (ord('s'), 13) and edit_state["zones"]:        # save
                save_zones(edit_state["zones"], frame, lot_id)
                break

            elif key == ord('e'):                                    # manual edit
                mode = "edit"
                edit_state["selected"] = None
                edit_state["pending"]  = []
                cv2.setMouseCallback(WINDOW_NAME, mouse_callback, edit_state)
                print("Entering manual edit mode.")

            elif key == ord('r'):                                    # retry sensitivity
                idx  = SENSITIVITY_CYCLE.index(sens)
                sens = SENSITIVITY_CYCLE[(idx + 1) % len(SENSITIVITY_CYCLE)]
                print("Retrying with sensitivity=%s ..." % sens)
                zones = detect_parking_spaces(frame, sens)
                edit_state["zones"]      = zones
                edit_state["pending"]    = []
                edit_state["selected"]   = None
                edit_state["next_index"] = len(zones) + 1
                label = "AUTO-DETECTED"
                print("Detected %d zones." % len(zones))

            elif key in (ord('q'), 27):                              # quit
                print("Exited without saving.")
                break

        # -- EDIT mode --
        elif mode == "edit":
            cv2.imshow(WINDOW_NAME, draw_edit(frame, edit_state))
            key = cv2.waitKey(20) & 0xFF

            if key in (ord('s'), 13) and edit_state["zones"]:        # save
                save_zones(edit_state["zones"], frame, lot_id)
                break

            elif key in (ord('x'), 127, 8):                          # delete selected zone
                if edit_state.get("selected") is not None:
                    delete_zone(edit_state, edit_state["selected"])
                else:
                    print("No zone selected. Click a zone first.")

            elif key == ord('d'):                                    # clear all
                edit_state["zones"].clear()
                edit_state["pending"].clear()
                edit_state["selected"]   = None
                edit_state["next_index"] = 1
                print("Cleared all zones.")

            elif key == ord('a'):                                    # back to review
                mode = "review"
                edit_state["selected"] = None
                edit_state["pending"]  = []
                print("Back to review mode.")

            elif key in (ord('q'), 27):                              # quit
                ans = input("Quit without saving? %d zone(s) will be lost. (y/N): "
                            % len(edit_state["zones"])).strip().lower()
                if ans == "y":
                    print("Exited without saving.")
                    break

    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
