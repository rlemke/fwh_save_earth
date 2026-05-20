"""Download EPA Toxic Release Inventory facility points.

Fetches the TRI_FACILITY table from
``https://data.epa.gov/efservice/`` (the new host; the old
``enviro.epa.gov`` URL 301-redirects there). Paginates transparently
past the 10,000-row per-request cap. Output lands at::

    $AFL_CACHE_ROOT/save-earth/tri/facilities.geojson + .meta.json

Longitude is stored unsigned in the TRI database — the download
library negates for western-hemisphere US/territory codes so the
output uses the GeoJSON convention (positive = east).

Usage::

    # Active facilities only (default)
    python download_tri.py

    # Include closed facilities too
    python download_tri.py --include-closed

    # Re-download even if cached
    python download_tri.py --force

    # Offline mock data for tests
    python download_tri.py --use-mock

About 65,000 facilities in the full DB; ~35,000 are currently active.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import tri  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--include-closed",
        action="store_true",
        help="Include closed facilities (fac_closed_ind='1'). Default: active-only.",
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if cached.")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=tri.DEFAULT_MAX_AGE_HOURS,
        help=(
            f"Cache freshness window (default: "
            f"{tri.DEFAULT_MAX_AGE_HOURS:.0f} h). TRI data refreshes roughly "
            f"annually, so a shorter window only matters for forced re-runs."
        ),
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

    try:
        res = tri.download(
            active_only=not args.include_closed,
            force=args.force,
            max_age_hours=args.max_age_hours,
            use_mock=args.use_mock,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    kind = "active-only" if res.active_only else "all (incl. closed)"
    print(
        f"[{status}] tri/{tri.RELATIVE_PATH}  "
        f"{res.feature_count:,} facilities ({kind})  "
        f"{res.size_bytes:,}B  sha256={res.sha256[:12]}…  "
        f"{res.absolute_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
