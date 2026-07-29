"""Download NASA FIRMS active-fire / thermal-anomaly detections (past 24 hours).

Merges the two keyless global feeds -- VIIRS S-NPP (375 m) and MODIS Terra/Aqua
(1 km) -- into one Point FeatureCollection, normalising each sensor's own
confidence onto a shared ``confidence_band`` (low/nominal/high) and sorting by
fire radiative power descending. Output lands at::

    $FW_CACHE_ROOT/save-earth/active_fire/active_fire.geojson + .meta.json

on whichever backend ``FW_STORAGE`` selects (``local`` / ``hdfs`` / ``s3``).

These are THERMAL ANOMALIES, not confirmed wildfires: gas flares, industrial
heat, active lava and agricultural burning all register the same way, and the
feed does not distinguish them. Detections are limited by cloud cover and
satellite overpass, so an empty area means "nothing detected", never "nothing
burning".

Usage::

    python download_wildfires.py                  # live FIRMS fetch (default)
    python download_wildfires.py --force          # re-download even if cached
    python download_wildfires.py --feeds viirs_snpp   # one sensor only
    python download_wildfires.py --use-mock       # offline mock data for tests
    FW_STORAGE=s3 python download_wildfires.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _save_earth_tools import wildfire  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if cached.")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=wildfire.MAX_AGE_HOURS,
        help=(
            f"Cache freshness window (default: {wildfire.MAX_AGE_HOURS:g} h). "
            f"This is near-real-time data -- a stale fire map is misleading in a "
            f"way a stale static layer is not."
        ),
    )
    parser.add_argument(
        "--feeds",
        default=",".join(wildfire.DEFAULT_FEEDS),
        help=(
            f"Comma-separated FIRMS feeds to merge. "
            f"Known: {','.join(sorted(wildfire.FEEDS))}. "
            f"Default: {','.join(wildfire.DEFAULT_FEEDS)}."
        ),
    )
    parser.add_argument(
        "--backend",
        default=None,
        help="Storage backend override (local/hdfs/s3). Default: $FW_STORAGE.",
    )
    parser.add_argument(
        "--use-mock",
        action="store_true",
        help="Deterministic offline data (no network).",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    feeds = tuple(f.strip() for f in args.feeds.split(",") if f.strip())
    unknown = [f for f in feeds if f not in wildfire.FEEDS]
    if unknown:
        print(
            f"error: unknown feed(s) {', '.join(unknown)}; "
            f"known: {', '.join(sorted(wildfire.FEEDS))}",
            file=sys.stderr,
        )
        return 2

    from _save_earth_tools.storage import get_storage

    try:
        res = wildfire.download_active_fire(
            force=args.force,
            max_age_hours=args.max_age_hours,
            storage=get_storage(args.backend) if args.backend else None,
            use_mock=args.use_mock,
            feeds=feeds,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    bands = res.band_counts or {}
    sensors = res.sensor_counts or {}
    print(
        f"[{status}] active_fire/{wildfire.RELATIVE_PATH}  "
        f"{res.feature_count:,} detections  {res.size_bytes:,}B  "
        f"sha256={res.sha256[:12]}...  {res.absolute_path}"
    )
    print(
        f"  confidence: high={bands.get('high', 0):,} "
        f"nominal={bands.get('nominal', 0):,} low={bands.get('low', 0):,}"
    )
    if sensors:
        print("  sensors: " + "  ".join(f"{k}={v:,}" for k, v in sorted(sensors.items())))
    if res.acquired_from:
        print(f"  acquired: {res.acquired_from} -> {res.acquired_to} (UTC)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
