"""
ParkWise — rename_zones.py
==========================

Renames the auto-generated S1…S27 zone IDs in zones.json (and status.json if
it exists) to the IDs the ParkWise app expects, based on the order the zones
were marked in mark_zones.py:

    S1 –S10  →  L01–L10   (left column,      marked top→bottom)
    S11–S20  →  R01–R10   (right column,     marked top→bottom)
    S21–S23  →  M01–M03   (centre top row,   marked left→right)
    S24–S27  →  N01–N04   (centre bottom row, marked left→right)

USAGE
-----
    py rename_zones.py                     # dry-run preview
    py rename_zones.py --apply             # write the renamed files

Run from the ParkWise-Vision directory (where zones.json lives).
"""

import argparse
import json
import sys
from pathlib import Path

ZONES_FILE  = Path("zones.json")
STATUS_FILE = Path("status.json")

# Build the canonical mapping  S<n> → target_id
# Order matches the marking order agreed with the user.
def build_mapping() -> dict[str, str]:
    mapping: dict[str, str] = {}
    n = 1
    for col in range(1, 11):          # L01–L10
        mapping[f"S{n}"] = f"L{col:02d}"
        n += 1
    for col in range(1, 11):          # R01–R10
        mapping[f"S{n}"] = f"R{col:02d}"
        n += 1
    for col in range(1, 4):           # M01–M03
        mapping[f"S{n}"] = f"M{col:02d}"
        n += 1
    for col in range(1, 5):           # N01–N04
        mapping[f"S{n}"] = f"N{col:02d}"
        n += 1
    return mapping


def rename_zones_file(mapping: dict[str, str], apply: bool) -> int:
    if not ZONES_FILE.exists():
        print(f"ERROR: {ZONES_FILE} not found — run mark_zones.py first.")
        return 1

    with ZONES_FILE.open(encoding="utf-8") as f:
        data = json.load(f)

    zones = data.get("zones", [])
    if not zones:
        print(f"ERROR: {ZONES_FILE} contains no zones.")
        return 1

    print(f"\nzones.json — {len(zones)} zones found")
    print(f"{'Old ID':<8}  {'New ID':<8}  corners")
    print("-" * 40)

    changed = 0
    unknown = []
    for z in zones:
        old = z["id"]
        new = mapping.get(old)
        if new is None:
            unknown.append(old)
            print(f"  {old:<8}  {'???':<8}  (no mapping — left unchanged)")
        else:
            print(f"  {old:<8}  {new:<8}")
            if apply:
                z["id"] = new
            changed += 1

    if unknown:
        print(f"\nWARNING: {len(unknown)} zone(s) have no mapping: {unknown}")

    if apply:
        with ZONES_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"\n✓ zones.json updated ({changed} zones renamed).")
    else:
        print(f"\nDry-run — pass --apply to write changes.")

    return 0


def rename_status_file(mapping: dict[str, str], apply: bool) -> None:
    if not STATUS_FILE.exists():
        print(f"\nstatus.json not found — skipping.")
        return

    with STATUS_FILE.open(encoding="utf-8") as f:
        data = json.load(f)

    spots = data.get("spots", [])
    changed = 0
    for sp in spots:
        old = sp["id"]
        new = mapping.get(old)
        if new:
            if apply:
                sp["id"] = new
            changed += 1

    if apply:
        with STATUS_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"✓ status.json updated ({changed} spots renamed).")
    else:
        print(f"status.json — {changed} spots would be renamed (dry-run).")


def main() -> int:
    p = argparse.ArgumentParser(description="Rename S1-S27 zone IDs to L/R/M/N scheme")
    p.add_argument("--apply", action="store_true",
                   help="Write changes to disk (default: dry-run only)")
    args = p.parse_args()

    mapping = build_mapping()

    print("Mapping table:")
    for src, dst in mapping.items():
        print(f"  {src} → {dst}", end="   ")
        if int(src[1:]) % 5 == 0:
            print()
    print()

    rc = rename_zones_file(mapping, args.apply)
    rename_status_file(mapping, args.apply)
    return rc


if __name__ == "__main__":
    sys.exit(main())
