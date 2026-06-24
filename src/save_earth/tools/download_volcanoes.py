"""Download major (notable) volcanoes worldwide from OpenStreetMap.

Queries the Overpass API for every ``natural=volcano`` feature that also
carries a ``wikidata`` or ``wikipedia`` tag (encyclopedic notability as the
"major" proxy), centroiding any way/relation to a Point. Every OSM tag is kept
verbatim as the feature's properties, so a downstream map popup can surface all
available information. Output lands at::

    $AFL_CACHE_ROOT/save-earth/volcanoes/volcanoes.geojson + .meta.json

on whichever backend ``AFL_STORAGE`` selects (``local`` / ``hdfs`` / ``s3``).
Coverage and tag completeness are OSM-community-driven and vary by region.

Usage::

    # Live Overpass fetch (default)
    python download_volcanoes.py

    # Re-download even if cached
    python download_volcanoes.py --force

    # Offline mock data for tests
    python download_volcanoes.py --use-mock

    # Write to the fleet object store
    AFL_STORAGE=s3 python download_volcanoes.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _save_earth_tools import volcanoes  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if cached.")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=volcanoes.DEFAULT_MAX_AGE_HOURS,
        help=(
            f"Cache freshness window (default: "
            f"{volcanoes.DEFAULT_MAX_AGE_HOURS:.0f} h). The volcano set changes "
            f"very slowly, so a shorter window only matters for forced re-runs."
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
        res = volcanoes.download(
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
        f"[{status}] volcanoes/{volcanoes.RELATIVE_PATH}  "
        f"{res.feature_count:,} volcanoes  "
        f"{res.size_bytes:,}B  sha256={res.sha256[:12]}…  "
        f"{res.absolute_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
