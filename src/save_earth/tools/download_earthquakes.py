"""Download recent significant earthquakes worldwide from the USGS feed.

Fetches the USGS real-time GeoJSON feed (default: magnitude 4.5+ over the past
30 days) and caches it as a Point FeatureCollection with the USGS properties
verbatim (``mag``, ``place``, ``time``, ``magType``, ``tsunami``, …) plus a
derived ``depth_km``. Output lands at::

    $AFL_CACHE_ROOT/save-earth/earthquakes/earthquakes.geojson + .meta.json

on whichever backend ``AFL_STORAGE`` selects (``local`` / ``hdfs`` / ``s3``).

Usage::

    python download_earthquakes.py            # live USGS fetch (default)
    python download_earthquakes.py --force     # re-download even if cached
    python download_earthquakes.py --use-mock  # offline mock data for tests
    AFL_STORAGE=s3 python download_earthquakes.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _save_earth_tools import seismic  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if cached.")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=seismic.EARTHQUAKES_MAX_AGE_HOURS,
        help=(
            f"Cache freshness window (default: "
            f"{seismic.EARTHQUAKES_MAX_AGE_HOURS:.0f} h). The feed updates "
            f"continuously, so a short window keeps the map current."
        ),
    )
    parser.add_argument(
        "--backend",
        default=None,
        help="Storage backend override (local/hdfs/s3). Default: $AFL_STORAGE.",
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
        res = seismic.download_earthquakes(
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
        f"[{status}] earthquakes/{seismic.EARTHQUAKES_RELATIVE_PATH}  "
        f"{res.feature_count:,} quakes  {res.size_bytes:,}B  "
        f"sha256={res.sha256[:12]}...  {res.absolute_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
