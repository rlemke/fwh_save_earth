"""Download worldwide ALPR surveillance cameras from OpenStreetMap (DeFlock data).

Queries the Overpass API for every node tagged
``man_made=surveillance`` + ``surveillance:type=ALPR`` — the crowd-sourced
Automatic License Plate Reader inventory the DeFlock project maintains in
OSM. This is camera LOCATIONS only (no video/plate data — that is private
to the operators). Every OSM tag is kept verbatim as the feature's
properties, plus a derived ``camera_vendor`` (flock/motorola/other), so a
downstream map popup can surface all available information. Output lands at::

    $FW_CACHE_ROOT/save-earth/alpr/cameras.geojson + .meta.json

on whichever backend ``FW_STORAGE`` selects (``local`` / ``hdfs`` /
``s3``). Coverage and tag completeness are OSM-community-driven and vary
by region (~336k nodes worldwide by early 2026).

Usage::

    # Live Overpass fetch (default)
    python download_alpr_cameras.py

    # Re-download even if cached
    python download_alpr_cameras.py --force

    # Offline mock data for tests
    python download_alpr_cameras.py --use-mock

    # Write to the fleet object store
    FW_STORAGE=s3 python download_alpr_cameras.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _save_earth_tools import alpr  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if cached.")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=alpr.DEFAULT_MAX_AGE_HOURS,
        help=(
            f"Cache freshness window (default: {alpr.DEFAULT_MAX_AGE_HOURS:.0f} h). "
            f"The crowd-sourced camera map updates continuously, so a shorter "
            f"window picks up new reports sooner."
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

    from _save_earth_tools.storage import get_storage

    try:
        res = alpr.download(
            force=args.force,
            max_age_hours=args.max_age_hours,
            storage=get_storage(args.backend) if args.backend else None,
            use_mock=args.use_mock,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    print(
        f"[{status}] alpr/{alpr.RELATIVE_PATH}  "
        f"{res.feature_count:,} ALPR cameras  "
        f"{res.size_bytes:,}B  sha256={res.sha256[:12]}…  "
        f"{res.absolute_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
