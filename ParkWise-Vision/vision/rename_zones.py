"""
ParkWise - rename_zones.py
==========================

Renames the auto-detected S1..S27 zone IDs (from auto_mark_zones.py) to the
IDs the ParkWise app's map expects.

auto_mark_zones.py numbers zones in this order (top->bottom, left->right):
    S1 -S10  ->  top row        (10 spaces)
    S11-S13  ->  left island    (3 spaces)
    S14-S17  ->  right island   (4 spaces)
    S18-S27  ->  bottom row     (10 spaces)

The app's map uses these IDs:
    L01-L10  =  left wall   (10)
    R01-R10  =  right wall  (10)
    M01-M03  =  centre upper island (3)
    N01-N04  =  centre lower island (4)

Mapping applied here:
    S1 -S10  ->  L01-L10   (top row)
    S11-S13  ->  M01-M03   (left island)
    S14-S17  ->  N01-N04   (right island)
    S18-S27  ->  R01-R10   (bottom row)

USAGE  (run from anywhere - paths are anchored to the ParkWise-Vision folder)
-----
    python rename_zones.py            # dry-run preview
    python rename_zones.py --apply    # write the renamed files
"""

import argparse
import json
import os
import sys

# Anchor file paths to the ParkWise-Vision folder (parent of this script's dir),
# so it works no matter which directory you run from.
DATA_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZONES_FILE  = os.path.join(DATA_DIR, "zones.json")
STATUS_FILE = os.path.join(DATA_DIR, "status.json")


def build_mapping():
    """S<n> -> app ID, matching auto_mark_zones.py's detection order."""
    mapping = {}
    n = 1
    for c in range(1, 11):   # S1 -S10 -> L01-L10  (top row)
        mapping[f"S{n}"] = f"L{c:02d}"; n += 1
    for c in range(1, 4):    # S11-S13 -> M01-M03  (left island)
        mapping[f"S{n}"] = f"M{c:02d}"; n += 1
    for c in range(1, 5):    # S14-S17 -> N01-N04  (right island)
        mapping[f"S{n}"] = f"N{c:02d}"; n += 1
    for c in range(1, 11):   # S18-S27 -> R01-R10  (bottom row)
        mapping[f"S{n}"] = f"R{c:02d}"; n += 1
    return mapping


def rename_zones_file(mapping, apply):
    if not os.path.exists(ZONES_FILE):
        print(f"ERROR: {ZONES_FILE} not found - run auto_mark_zones.py first.")
        return 1

    with open(ZONES_FILE, encoding="utf-8") as f:
        data = json.load(f)

    zones = data.get("zones", [])
    if not zones:
        print(f"ERROR: {ZONES_FILE} contains no zones.")
        return 1

    print(f"\nzones.json - {len(zones)} zones found")
    print(f"{'Old ID':<8}{'New ID':<8}")
    print("-" * 24)

    changed, unknown = 0, []
    for z in zones:
        old = z["id"]
        new = mapping.get(old)
        if new is None:
            unknown.append(old)
            print(f"  {old:<8}{'???':<8}(no mapping - left unchanged)")
        else:
            print(f"  {old:<8}{new:<8}")
            if apply:
                z["id"] = new
            changed += 1

    if unknown:
        print(f"\nWARNING: {len(unknown)} zone(s) have no mapping: {unknown}")

    if apply:
        with open(ZONES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"\nOK - zones.json updated ({changed} zones renamed).")
    else:
        print(f"\nDry-run - pass --apply to write changes.")
    return 0


def rename_status_file(mapping, apply):
    if not os.path.exists(STATUS_FILE):
        print("\nstatus.json not found - skipping.")
        return
    with open(STATUS_FILE, encoding="utf-8") as f:
        data = json.load(f)
    spots = data.get("spots", [])
    changed = 0
    for sp in spots:
        new = mapping.get(sp["id"])
        if new:
            if apply:
                sp["id"] = new
            changed += 1
    if apply:
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"OK - status.json updated ({changed} spots renamed).")
    else:
        print(f"status.json - {changed} spots would be renamed (dry-run).")


def main():
    p = argparse.ArgumentParser(description="Rename S1-S27 zone IDs to the app's L/R/M/N scheme")
    p.add_argument("--apply", action="store_true",
                   help="Write changes to disk (default: dry-run only)")
    args = p.parse_args()

    mapping = build_mapping()

    print("Mapping table:")
    for i, (src, dst) in enumerate(mapping.items(), 1):
        print(f"  {src} -> {dst}", end="   ")
        if i % 5 == 0:
            print()
    print()

    rc = rename_zones_file(mapping, args.apply)
    rename_status_file(mapping, args.apply)
    return rc


if __name__ == "__main__":
    sys.exit(main())
