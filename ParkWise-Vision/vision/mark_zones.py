"""
ParkWise — Task 1.3: Parking Zone Marking Tool
================================================

Lets you manually mark parking zones on a webcam frame (or static image)
once per session, then saves them to JSON for downstream occupancy detection.

USAGE
-----
    python mark_zones.py --camera 0 --lot-id demo_lot_1
    python mark_zones.py --image path/to/frame.jpg --lot-id demo_lot_1

CONTROLS
--------
    [Live preview phase, camera mode only]
    SPACE        : freeze the current frame and start marking
    Q / ESC      : abort

    [Marking phase]
    Left click   : add a corner point (every 4 clicks completes a zone)
    Right click  : undo last point (or remove last finalized zone if no points pending)
    R            : reset everything (clear all zones)
    S            : save zones.json + baseline.jpg, then exit
    Q / ESC      : quit without saving (asks for confirmation)
    SPACE        : re-open live preview to re-capture the frame

OUTPUT
------
    zones.json    — list of zones with quadrilateral polygons in pixel coords
    baseline.jpg  — the exact frame the zones were drawn on (used by Task 1.4
                    as the background-subtraction reference)

Both files are written to the current working directory.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Constants — tweak freely
# ---------------------------------------------------------------------------
WINDOW_NAME = "ParkWise - Mark Zones"
ZONES_FILE = "zones.json"
BASELINE_FILE = "baseline.jpg"

POINTS_PER_ZONE = 4

# Colors are BGR (OpenCV convention)
COLOR_PENDING_POINT = (0, 165, 255)   # orange
COLOR_PENDING_LINE = (0, 200, 255)    # yellow-orange
COLOR_ZONE_FILL = (0, 200, 0)         # green
COLOR_ZONE_OUTLINE = (0, 180, 0)      # darker green
COLOR_ID_TEXT = (255, 255, 255)       # white
COLOR_ID_OUTLINE = (0, 0, 0)          # black
COLOR_STATUS_BG = (40, 40, 40)        # dark gray
COLOR_STATUS_TEXT = (240, 240, 240)   # off-white

POINT_RADIUS = 5
LINE_THICKNESS = 2
ZONE_OUTLINE_THICKNESS = 2
ZONE_FILL_ALPHA = 0.30
FONT = cv2.FONT_HERSHEY_SIMPLEX
ID_FONT_SCALE = 0.7
ID_FONT_THICKNESS = 2
STATUS_FONT_SCALE = 0.55
STATUS_FONT_THICKNESS = 1
STATUS_BAR_HEIGHT = 32


# ---------------------------------------------------------------------------
# State (kept in a dict so the mouse callback can mutate it cleanly)
# ---------------------------------------------------------------------------
def make_state():
    return {
        "zones": [],          # list of {"id": str, "polygon": [[x,y]*4]}
        "pending": [],        # list of [x, y] for the in-progress zone
        "next_index": 1,      # auto-increment counter for zone IDs
    }


def next_zone_id(state):
    return f"S{state['next_index']}"


# ---------------------------------------------------------------------------
# Capture / IO
# ---------------------------------------------------------------------------
def live_capture_frame(camera_index):
    """
    Show a live preview window so the user can position the camera/scene,
    then freeze the frame on SPACE. Returns the captured BGR frame or None
    on abort/error.
    """
    # CAP_DSHOW is the reliable backend on Windows (matches camera_test.py)
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

            # Overlay a hint banner so the user knows what to do
            preview = frame.copy()
            h, w = preview.shape[:2]
            cv2.rectangle(preview, (0, 0), (w, STATUS_BAR_HEIGHT), COLOR_STATUS_BG, -1)
            hint = "LIVE - Position camera, then SPACE = capture, Q = abort"
            cv2.putText(preview, hint, (8, 21), FONT, STATUS_FONT_SCALE,
                        COLOR_STATUS_TEXT, STATUS_FONT_THICKNESS, cv2.LINE_AA)

            cv2.imshow(WINDOW_NAME, preview)
            key = cv2.waitKey(1) & 0xFF
            if key == ord(' '):
                captured = frame.copy()
                print("Frame captured.")
                break
            if key == ord('q') or key == 27:  # 27 = ESC
                print("Live preview aborted.")
                break
    finally:
        cap.release()
    return captured


def save_zones(state, image, lot_id):
    """Write zones.json and baseline.jpg to CWD."""
    h, w = image.shape[:2]
    payload = {
        "lot_id": lot_id,
        "image_size": {"width": int(w), "height": int(h)},
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "zones": [
            {"id": z["id"], "polygon": [[int(x), int(y)] for x, y in z["polygon"]]}
            for z in state["zones"]
        ],
    }
    with open(ZONES_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    cv2.imwrite(BASELINE_FILE, image)
    print(f"Saved {len(state['zones'])} zones -> {ZONES_FILE}")
    print(f"Saved baseline frame -> {BASELINE_FILE}")


def load_zones(state):
    """Populate state from an existing zones.json. Returns True on success."""
    try:
        with open(ZONES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"Could not read existing {ZONES_FILE}: {e}")
        return False

    state["zones"] = [
        {"id": z["id"], "polygon": [list(map(int, p)) for p in z["polygon"]]}
        for z in data.get("zones", [])
    ]
    # Restart numbering past any S<N> we recovered, so new IDs don't collide
    max_n = 0
    for z in state["zones"]:
        zid = z["id"]
        if zid.startswith("S") and zid[1:].isdigit():
            max_n = max(max_n, int(zid[1:]))
    state["next_index"] = max_n + 1
    return True


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def polygon_centroid(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return int(sum(xs) / len(xs)), int(sum(ys) / len(ys))


def draw_label(img, text, center):
    """Draw text with a black outline so it's readable on any background."""
    (tw, th), _ = cv2.getTextSize(text, FONT, ID_FONT_SCALE, ID_FONT_THICKNESS)
    org = (center[0] - tw // 2, center[1] + th // 2)
    cv2.putText(img, text, org, FONT, ID_FONT_SCALE, COLOR_ID_OUTLINE,
                ID_FONT_THICKNESS + 2, cv2.LINE_AA)
    cv2.putText(img, text, org, FONT, ID_FONT_SCALE, COLOR_ID_TEXT,
                ID_FONT_THICKNESS, cv2.LINE_AA)


def draw_overlay(base_image, state):
    """Return a fresh copy of base_image with all zones + pending points rendered."""
    canvas = base_image.copy()

    # Semi-transparent fills for finalized zones
    if state["zones"]:
        fill_layer = canvas.copy()
        for z in state["zones"]:
            pts = np.array(z["polygon"], dtype=np.int32)
            cv2.fillPoly(fill_layer, [pts], COLOR_ZONE_FILL)
        cv2.addWeighted(fill_layer, ZONE_FILL_ALPHA, canvas,
                        1 - ZONE_FILL_ALPHA, 0, canvas)

        # Outlines + labels on top of the blended fill
        for z in state["zones"]:
            pts = np.array(z["polygon"], dtype=np.int32)
            cv2.polylines(canvas, [pts], True, COLOR_ZONE_OUTLINE,
                          ZONE_OUTLINE_THICKNESS, cv2.LINE_AA)
            draw_label(canvas, z["id"], polygon_centroid(z["polygon"]))

    # Pending points + connecting lines
    pending = state["pending"]
    for i, p in enumerate(pending):
        cv2.circle(canvas, tuple(p), POINT_RADIUS, COLOR_PENDING_POINT, -1, cv2.LINE_AA)
        if i > 0:
            cv2.line(canvas, tuple(pending[i - 1]), tuple(p),
                     COLOR_PENDING_LINE, LINE_THICKNESS, cv2.LINE_AA)

    # Status bar across the top
    h, w = canvas.shape[:2]
    cv2.rectangle(canvas, (0, 0), (w, STATUS_BAR_HEIGHT), COLOR_STATUS_BG, -1)
    n_done = len(state["zones"])
    n_pending = len(pending)
    if n_pending == 0:
        hint = "Left-click to start a new zone"
    elif n_pending < POINTS_PER_ZONE:
        hint = f"Click {POINTS_PER_ZONE - n_pending} more corner(s)"
    else:
        hint = "..."
    status = (f"Zones: {n_done}   "
              f"Current corners: {n_pending}/{POINTS_PER_ZONE}   "
              f"[{hint}]   "
              f"S=save  R=reset  Q=quit")
    cv2.putText(canvas, status, (8, 21), FONT, STATUS_FONT_SCALE,
                COLOR_STATUS_TEXT, STATUS_FONT_THICKNESS, cv2.LINE_AA)

    return canvas


# ---------------------------------------------------------------------------
# Mouse callback
# ---------------------------------------------------------------------------
def mouse_callback(event, x, y, flags, state):
    if event == cv2.EVENT_LBUTTONDOWN:
        state["pending"].append([x, y])
        if len(state["pending"]) == POINTS_PER_ZONE:
            zone_id = next_zone_id(state)
            state["zones"].append({"id": zone_id, "polygon": state["pending"]})
            state["pending"] = []
            state["next_index"] += 1
            print(f"Added zone {zone_id}")
    elif event == cv2.EVENT_RBUTTONDOWN:
        if state["pending"]:
            state["pending"].pop()
        elif state["zones"]:
            removed = state["zones"].pop()
            state["next_index"] = max(1, state["next_index"] - 1)
            print(f"Removed zone {removed['id']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def prompt_resume():
    """Prompt user when zones.json already exists. Returns 'load', 'overwrite', or 'quit'."""
    try:
        with open(ZONES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        n = len(data.get("zones", []))
    except (OSError, json.JSONDecodeError):
        return "overwrite"

    while True:
        ans = input(f"Existing {ZONES_FILE} found with {n} zones. "
                    "(L)oad and continue editing, (O)verwrite from scratch, or (Q)uit? ").strip().lower()
        if ans in ("l", "load"):
            return "load"
        if ans in ("o", "overwrite"):
            return "overwrite"
        if ans in ("q", "quit"):
            return "quit"
        print("Please answer L, O, or Q.")


def parse_args():
    p = argparse.ArgumentParser(description="ParkWise zone marking tool")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--camera", type=int, default=1, help="Webcam index (default 1 — Logitech on this machine)")
    src.add_argument("--image", type=str, help="Path to a static image to mark on")
    p.add_argument("--lot-id", type=str, default="demo_lot_1", help="lot_id field in zones.json")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--overwrite", action="store_true", help="Always start fresh (skip L/O/Q prompt)")
    mode.add_argument("--load", action="store_true", help="Always load existing zones.json (skip prompt)")
    return p.parse_args()


def main():
    args = parse_args()

    print(__doc__)

    state = make_state()

    # Resume-mode prompt
    if os.path.exists(ZONES_FILE):
        if args.overwrite:
            choice = "overwrite"
        elif args.load:
            choice = "load"
        else:
            choice = prompt_resume()
        if choice == "quit":
            return 0
        if choice == "load":
            if not load_zones(state):
                print("Falling back to overwrite mode.")
                state = make_state()

    # Acquire base image
    if args.image:
        base = cv2.imread(args.image)
        if base is None:
            print(f"Error: could not read image '{args.image}'.")
            return 1
        camera_mode = False
    else:
        base = live_capture_frame(args.camera)
        if base is None:
            return 1
        camera_mode = True

    cv2.namedWindow(WINDOW_NAME)
    cv2.setMouseCallback(WINDOW_NAME, mouse_callback, state)

    while True:
        cv2.imshow(WINDOW_NAME, draw_overlay(base, state))
        key = cv2.waitKey(20) & 0xFF

        if key == ord('s'):
            save_zones(state, base, args.lot_id)
            break
        elif key == ord('r'):
            state["zones"].clear()
            state["pending"].clear()
            state["next_index"] = 1
            print("Reset: cleared all zones.")
        elif key == ord(' ') and camera_mode:
            # Re-open the live preview so the user can re-position before re-capturing.
            # The marking window is destroyed first because live_capture_frame opens its own.
            cv2.destroyWindow(WINDOW_NAME)
            new_frame = live_capture_frame(args.camera)
            if new_frame is not None:
                base = new_frame
                print("Re-captured frame.")
            # Re-bind the mouse callback on the new window instance
            cv2.namedWindow(WINDOW_NAME)
            cv2.setMouseCallback(WINDOW_NAME, mouse_callback, state)
        elif key == ord('q') or key == 27:  # 27 = ESC
            ans = input(f"Quit without saving? {len(state['zones'])} zone(s) will be lost. (y/N): ").strip().lower()
            if ans == "y":
                print("Exited without saving.")
                break
            # else fall through and keep editing

    cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
