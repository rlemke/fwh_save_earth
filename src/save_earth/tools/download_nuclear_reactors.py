"""Download worldwide nuclear power reactors & plants from OpenStreetMap.

Queries the Overpass API for every feature tagged
``generator:source=nuclear`` (an individual reactor) or
``plant:source=nuclear`` (a whole station), centroiding any way/relation
polygon to a Point. Every OSM tag is kept verbatim as the feature's
properties, so a downstream map popup can surface all available
information. Output lands at::

    $FW_CACHE_ROOT/save-earth/nuclear/reactors.geojson + .meta.json

on whichever backend ``FW_STORAGE`` selects (``local`` / ``hdfs`` /
``s3``). Coverage and tag completeness are OSM-community-driven and vary
by country.

Usage::

    # Live Overpass fetch (default)
    python download_nuclear_reactors.py

    # Re-download even if cached
    python download_nuclear_reactors.py --force

    # Offline mock data for tests
    python download_nuclear_reactors.py --use-mock

    # Write to the fleet object store
    FW_STORAGE=s3 python download_nuclear_reactors.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _save_earth_tools import nuclear  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if cached.")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=nuclear.DEFAULT_MAX_AGE_HOURS,
        help=(
            f"Cache freshness window (default: "
            f"{nuclear.DEFAULT_MAX_AGE_HOURS:.0f} h). The reactor fleet changes "
            f"slowly, so a shorter window only matters for forced re-runs."
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
        res = nuclear.download(
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
        f"[{status}] nuclear/{nuclear.RELATIVE_PATH}  "
        f"{res.feature_count:,} reactors/plants  "
        f"{res.size_bytes:,}B  sha256={res.sha256[:12]}…  "
        f"{res.absolute_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
