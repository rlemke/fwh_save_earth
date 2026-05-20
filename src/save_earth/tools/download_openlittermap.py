"""Download OpenLitterMap geotagged litter observations.

OpenLitterMap's public API has two GeoJSON endpoints:

- ``clusters`` (default): aggregate clusters, any zoom, global bbox optional.
- ``points``: individual photos with full per-observation metadata;
  requires ``zoom >= 15`` AND a bbox (server-enforced).

Outputs land at
``$AFL_CACHE_ROOT/save-earth/openlittermap/<mode>-zoom<N>[_<bbox>].geojson``
plus a sibling ``.meta.json`` sidecar.

Usage::

    # Global cluster overview (default)
    python download_openlittermap.py

    # Custom zoom level for clusters (higher = more / smaller clusters)
    python download_openlittermap.py --zoom 6

    # Individual points for a small area (manhattan ≈ zoom 15)
    python download_openlittermap.py --mode points \\
        --bbox -74.02,40.70,-73.97,40.75 --zoom 15

    # Offline mock data
    python download_openlittermap.py --use-mock
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import openlittermap  # noqa: E402


def _parse_bbox(s: str) -> tuple[float, float, float, float]:
    try:
        parts = [float(p) for p in s.split(",")]
    except ValueError as exc:
        raise SystemExit(f"error: --bbox needs 4 comma-separated numbers: {exc}") from exc
    if len(parts) != 4:
        raise SystemExit("error: --bbox needs 4 values: min_lon,min_lat,max_lon,max_lat")
    return tuple(parts)  # type: ignore[return-value]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["clusters", "points"],
        default=openlittermap.DEFAULT_MODE,
        help=(
            f"clusters = aggregate (any zoom); points = individual photos "
            f"(requires zoom>={openlittermap.MIN_POINTS_ZOOM}, requires --bbox). "
            f"Default: {openlittermap.DEFAULT_MODE}."
        ),
    )
    parser.add_argument(
        "--zoom",
        type=int,
        default=openlittermap.DEFAULT_ZOOM,
        help=f"Zoom level 0–24 (default {openlittermap.DEFAULT_ZOOM}).",
    )
    parser.add_argument(
        "--bbox",
        default=None,
        help="min_lon,min_lat,max_lon,max_lat — required for --mode points.",
    )
    parser.add_argument(
        "--url",
        default=openlittermap.DEFAULT_BASE_URL,
        help=f"Upstream API base (default: {openlittermap.DEFAULT_BASE_URL}).",
    )
    parser.add_argument("--force", action="store_true", help="Re-download even if cached.")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=openlittermap.DEFAULT_MAX_AGE_HOURS,
    )
    parser.add_argument(
        "--use-mock",
        action="store_true",
        help="Opt in to deterministic mock data (no network).",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    bbox = _parse_bbox(args.bbox) if args.bbox else None

    try:
        res = openlittermap.download(
            mode=args.mode,
            zoom=args.zoom,
            bbox=bbox,
            url=args.url,
            force=args.force,
            max_age_hours=args.max_age_hours,
            use_mock=args.use_mock,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    print(
        f"[{status}] openlittermap/{res.relative_path}  "
        f"{res.feature_count:,} features  {res.size_bytes:,}B  "
        f"sha256={res.sha256[:12]}…  {res.absolute_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
