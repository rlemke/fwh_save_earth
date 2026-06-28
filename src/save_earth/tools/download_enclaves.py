"""Download worldwide heritage-named enclave neighbourhoods from OpenStreetMap.

Queries the Overpass API for named places (``place=neighbourhood`` / ``quarter``
/ ``suburb`` / ``city_block`` / ``locality``) whose **name** marks a heritage
enclave — "Chinatown", "Japantown", "Little Italy", "Koreatown", "Little Saigon",
"Greektown", "Little Havana", … OSM has no structured ethnicity attribute, so the
*name* is the signal. Each match is classified into a heritage bucket and written
to one GeoJSON FeatureCollection per heritage::

    $FW_CACHE_ROOT/save-earth/enclaves/<slug>.geojson + .meta.json

on whichever backend ``FW_STORAGE`` selects (``local`` / ``hdfs`` / ``s3``).
Coverage is OSM-community-driven and uneven (rich in US/European metros), and
name-matching admits the odd false positive — honest limits of an open source.

Usage::

    python download_enclaves.py                 # live Overpass fetch
    python download_enclaves.py --force          # re-download even if cached
    python download_enclaves.py --use-mock       # offline mock data for tests
    FW_STORAGE=s3 python download_enclaves.py    # write to the fleet object store
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _save_earth_tools import enclaves  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if cached.")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=enclaves.DEFAULT_MAX_AGE_HOURS,
        help=(
            f"Cache freshness window (default: {enclaves.DEFAULT_MAX_AGE_HOURS:.0f} h). "
            f"Enclave place names change very slowly."
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
        res = enclaves.download(
            force=args.force,
            max_age_hours=args.max_age_hours,
            storage=get_storage(args.backend) if args.backend else None,
            use_mock=args.use_mock,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    top = ", ".join(
        f"{k}={v}" for k, v in sorted(res.per_heritage.items(), key=lambda x: -x[1])[:6]
    )
    print(
        f"[{status}] enclaves  {res.feature_count:,} places across "
        f"{res.heritage_count} heritages  ({top})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
