"""
ParkWise — Task 1.4: Parking Occupancy Detector
================================================

Reads zones.json + baseline.jpg (produced by mark_zones.py) and decides, for
every zone in every frame, whether it is occupied or free. Method: per-zone
mean absolute pixel difference between current frame and baseline (background
subtraction with a Gaussian blur to suppress noise).

USAGE
-----
    python detect_occupancy.py                          # live camera (Logitech, index 1)
    python detect_occupancy.py --camera 0               # different camera
    python detect_occupancy.py --video clip.mp4         # replay a recorded clip
    python detect_occupancy.py --image frame.jpg        # one-shot detection on a still
    python detect_occupancy.py --no-window              # headless, JSON to stdout + file
    python detect_occupancy.py --threshold 30           # custom threshold
    python detect_occupancy.py --output status.json     # custom JSON output path

CONTROLS (window mode)
----------------------
    Q / ESC : quit
    + / -   : adjust threshold live by 5
    S       : save the annotated frame as debug_snapshot.jpg
    B       : re-capture baseline from the current frame (also overwrites baseline.jpg)

OUTPUT
------
    status.json : current per-spot status, refreshed every frame in window mode
                  (~2 Hz in --no-window mode). Schema is documented in make_payload().
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

import cv2
import numpy as np
import requests


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BACKEND_URL = "http://vmedu471.mtacloud.co.il:3000/api/spots/update"

WINDOW_NAME = "ParkWise - Occupancy Detector"
ZONES_FILE = "zones.json"
BASELINE_FILE = "baseline.jpg"
DEBUG_SNAPSHOT_FILE = "debug_snapshot.jpg"

# BGR
COLOR_FREE = (0, 200, 0)
COLOR_OCCUPIED = (0, 50, 220)
COLOR_TEXT = (255, 255, 255)
COLOR_TEXT_OUTLINE = (0, 0, 0)
COLOR_STATUS_BG = (40, 40, 40)
COLOR_STATUS_TEXT = (240, 240, 240)

ZONE_FILL_ALPHA = 0.35
ZONE_OUTLINE_THICKNESS = 2

FONT = cv2.FONT_HERSHEY_SIMPLEX
ID_FONT_SCALE = 0.65
ID_FONT_THICKNESS = 2
SCORE_FONT_SCALE = 0.45
SCORE_FONT_THICKNESS = 1
STATUS_FONT_SCALE = 0.55
STATUS_FONT_THICKNESS = 1
STATUS_BAR_HEIGHT = 32

GAUSS_KSIZE = (5, 5)
THRESHOLD_STEP = 5
HEADLESS_JSON_HZ = 2.0
POST_INTERVAL = 3.0  # seconds between backend POSTs in window mode


# ---------------------------------------------------------------------------
# IO
# ---------------------------------------------------------------------------
def load_zones():
    if not os.path.exists(ZONES_FILE):
        sys.exit(f"Error: {ZONES_FILE} not found in CWD. Run mark_zones.py first.")
    with open(ZONES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    zones = data.get("zones", [])
    if not zones:
        sys.exit(f"Error: {ZONES_FILE} contains no zones.")
    return data.get("lot_id", "unknown_lot"), zones


def load_baseline():
    if not os.path.exists(BASELINE_FILE):
        sys.exit(f"Error: {BASELINE_FILE} not found in CWD. Run mark_zones.py first.")
    img = cv2.imread(BASELINE_FILE)
    if img is None:
        sys.exit(f"Error: could not decode {BASELINE_FILE}.")
    return img


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def post_to_backend(payload):
    try:
        requests.post(BACKEND_URL, json=payload, timeout=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Detection core
# ---------------------------------------------------------------------------
def preprocess(frame):
    """BGR -> blurred grayscale. Used identically on baseline and current frame."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(gray, GAUSS_KSIZE, 0)


def precompute_masks(zones, shape_hw):
    """
    Build a uint8 binary mask per zone (255 inside, 0 outside) plus a centroid
    for label placement. Done once at startup — recomputing each frame is the
    difference between 30 FPS and a slideshow.
    """
    masks = {}
    centroids = {}
    for z in zones:
        mask = np.zeros(shape_hw, dtype=np.uint8)
        pts = np.array(z["polygon"], dtype=np.int32)
        cv2.fillPoly(mask, [pts], 255)
        masks[z["id"]] = mask
        xs = [p[0] for p in z["polygon"]]
        ys = [p[1] for p in z["polygon"]]
        centroids[z["id"]] = (int(sum(xs) / len(xs)), int(sum(ys) / len(ys)))
    return masks, centroids


def detect_zone(diff, mask):
    """Mean absolute pixel difference within the masked polygon region."""
    return float(cv2.mean(diff, mask=mask)[0])


def process_frame(frame, base_gray, masks, zones, threshold):
    """Returns dict zone_id -> (score, status)."""
    cur_gray = preprocess(frame)
    diff = cv2.absdiff(base_gray, cur_gray)
    results = {}
    for z in zones:
        zid = z["id"]
        score = detect_zone(diff, masks[zid])
        status = "occupied" if score > threshold else "free"
        results[zid] = (score, status)
    return results


def make_payload(lot_id, threshold, zones, results):
    spots = []
    occupied = 0
    for z in zones:
        zid = z["id"]
        score, status = results[zid]
        if status == "occupied":
            occupied += 1
        spots.append({"id": zid, "status": status, "score": round(score, 2)})
    total = len(zones)
    return {
        "lot_id": lot_id,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "threshold": threshold,
        "spots": spots,
        "summary": {"total": total, "occupied": occupied, "free": total - occupied},
    }


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------
def draw_label(img, lines, center, scales=(ID_FONT_SCALE, SCORE_FONT_SCALE),
               thicks=(ID_FONT_THICKNESS, SCORE_FONT_THICKNESS)):
    """Draw stacked text lines centered at `center`, each with a black outline."""
    sizes = [cv2.getTextSize(t, FONT, s, th)[0] for t, s, th in zip(lines, scales, thicks)]
    total_h = sum(h for _, h in sizes) + 4 * (len(lines) - 1)
    y = center[1] - total_h // 2
    for text, (tw, th), scale, thick in zip(lines, sizes, scales, thicks):
        org = (center[0] - tw // 2, y + th)
        cv2.putText(img, text, org, FONT, scale, COLOR_TEXT_OUTLINE,
                    thick + 2, cv2.LINE_AA)
        cv2.putText(img, text, org, FONT, scale, COLOR_TEXT,
                    thick, cv2.LINE_AA)
        y += th + 4


def draw_overlay(frame, zones, results, centroids, fps, threshold, summary):
    canvas = frame.copy()

    # Single alpha-blend pass for all fills
    fill_layer = canvas.copy()
    for z in zones:
        score, status = results[z["id"]]
        color = COLOR_OCCUPIED if status == "occupied" else COLOR_FREE
        pts = np.array(z["polygon"], dtype=np.int32)
        cv2.fillPoly(fill_layer, [pts], color)
    cv2.addWeighted(fill_layer, ZONE_FILL_ALPHA, canvas, 1 - ZONE_FILL_ALPHA, 0, canvas)

    # Outlines + labels on top
    for z in zones:
        zid = z["id"]
        score, status = results[zid]
        color = COLOR_OCCUPIED if status == "occupied" else COLOR_FREE
        pts = np.array(z["polygon"], dtype=np.int32)
        cv2.polylines(canvas, [pts], True, color, ZONE_OUTLINE_THICKNESS, cv2.LINE_AA)
        draw_label(canvas, [zid, f"{score:.1f}"], centroids[zid])

    # Status bar
    h, w = canvas.shape[:2]
    cv2.rectangle(canvas, (0, 0), (w, STATUS_BAR_HEIGHT), COLOR_STATUS_BG, -1)
    status_text = (f"Total: {summary['total']}   "
                   f"Occupied: {summary['occupied']}   "
                   f"Free: {summary['free']}   "
                   f"Threshold: {threshold}   "
                   f"FPS: {fps:.1f}   "
                   f"[Q=quit  +/-=threshold  S=snap  B=rebaseline]")
    cv2.putText(canvas, status_text, (8, 21), FONT, STATUS_FONT_SCALE,
                COLOR_STATUS_TEXT, STATUS_FONT_THICKNESS, cv2.LINE_AA)
    return canvas


# ---------------------------------------------------------------------------
# Source / args
# ---------------------------------------------------------------------------
def open_video_source(args):
    if args.video:
        cap = cv2.VideoCapture(args.video)
    else:
        cap = cv2.VideoCapture(args.camera, cv2.CAP_DSHOW)
    if not cap.isOpened():
        sys.exit("Error: could not open video source.")
    return cap


def parse_args():
    p = argparse.ArgumentParser(description="ParkWise occupancy detector")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--camera", type=int, default=1,
                     help="Webcam index (default 1 — Logitech on this machine)")
    src.add_argument("--video", type=str, help="Path to video file (replay mode)")
    src.add_argument("--image", type=str, help="Path to single image (one-shot mode)")
    p.add_argument("--threshold", type=int, default=25,
                   help="Mean diff above which a zone counts as occupied (default 25)")
    p.add_argument("--no-window", action="store_true",
                   help="Headless mode — JSON only, no debug window")
    p.add_argument("--output", type=str, default="status.json",
                   help="JSON output path (default status.json)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args = parse_args()

    lot_id, zones = load_zones()
    baseline = load_baseline()
    base_gray = preprocess(baseline)
    h, w = baseline.shape[:2]
    masks, centroids = precompute_masks(zones, (h, w))
    threshold = args.threshold

    print(f"Loaded {len(zones)} zones from {ZONES_FILE} (lot_id={lot_id})")
    print(f"Baseline: {w}x{h}")
    print(f"Threshold: {threshold}")
    if args.image:
        print("Mode: single image")
    elif args.video:
        print(f"Mode: video file ({args.video})")
    else:
        print(f"Mode: live camera (index {args.camera})")
    if args.no_window:
        print("Headless: JSON only, no debug window")

    # ---- Single-image mode: detect once, output, exit ----
    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            sys.exit(f"Error: could not read {args.image}")
        if frame.shape[:2] != (h, w):
            print(f"Warning: image is {frame.shape[1]}x{frame.shape[0]} but "
                  f"baseline is {w}x{h}. Resizing to match.")
            frame = cv2.resize(frame, (w, h))
        results = process_frame(frame, base_gray, masks, zones, threshold)
        payload = make_payload(lot_id, threshold, zones, results)
        write_json(args.output, payload)
        post_to_backend(payload)
        print(json.dumps(payload, indent=2))
        if not args.no_window:
            canvas = draw_overlay(frame, zones, results, centroids, 0.0, threshold, payload["summary"])
            cv2.imshow(WINDOW_NAME, canvas)
            print("Press any key in the window to exit.")
            cv2.waitKey(0)
            cv2.destroyAllWindows()
        return 0

    # ---- Live / video stream mode ----
    cap = open_video_source(args)
    if not args.no_window:
        cv2.namedWindow(WINDOW_NAME)

    last_t = time.time()
    fps_ema = 0.0
    last_json_write = 0.0
    last_post_time = 0.0

    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                if args.video:
                    print("Video ended.")
                    break
                print("Warning: failed to read frame, retrying.")
                continue

            if frame.shape[:2] != (h, w):
                frame = cv2.resize(frame, (w, h))

            results = process_frame(frame, base_gray, masks, zones, threshold)
            payload = make_payload(lot_id, threshold, zones, results)

            now = time.time()
            dt = now - last_t
            last_t = now
            if dt > 0:
                inst_fps = 1.0 / dt
                fps_ema = inst_fps if fps_ema == 0 else 0.9 * fps_ema + 0.1 * inst_fps

            # JSON output
            if args.no_window:
                if now - last_json_write >= 1.0 / HEADLESS_JSON_HZ:
                    write_json(args.output, payload)
                    post_to_backend(payload)
                    print(json.dumps(payload), flush=True)
                    last_json_write = now
            else:
                write_json(args.output, payload)
                if now - last_post_time >= POST_INTERVAL:
                    post_to_backend(payload)
                    last_post_time = now

            # Window + keys
            if not args.no_window:
                canvas = draw_overlay(frame, zones, results, centroids, fps_ema,
                                      threshold, payload["summary"])
                cv2.imshow(WINDOW_NAME, canvas)
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q') or key == 27:
                    break
                elif key == ord('s'):
                    cv2.imwrite(DEBUG_SNAPSHOT_FILE, canvas)
                    print(f"Saved {DEBUG_SNAPSHOT_FILE}")
                elif key in (ord('+'), ord('=')):
                    threshold += THRESHOLD_STEP
                    print(f"Threshold -> {threshold}")
                elif key in (ord('-'), ord('_')):
                    threshold = max(0, threshold - THRESHOLD_STEP)
                    print(f"Threshold -> {threshold}")
                elif key == ord('b'):
                    baseline = frame.copy()
                    base_gray = preprocess(baseline)
                    cv2.imwrite(BASELINE_FILE, baseline)
                    print(f"Baseline rebaselined and saved -> {BASELINE_FILE}")
            # Headless: Ctrl-C to exit
    except KeyboardInterrupt:
        print("Interrupted.")
    finally:
        cap.release()
        if not args.no_window:
            cv2.destroyAllWindows()

    return 0


if __name__ == "__main__":
    sys.exit(main())
