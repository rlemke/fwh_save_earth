"""Download EPA cleanup-site datasets (Superfund / Brownfields / RCRA).

Each dataset lands at
``$AFL_CACHE_ROOT/save-earth/epa-cleanups/<dataset>.geojson`` with a
sibling ``.meta.json`` sidecar.

Usage::

    # All three datasets (default)
    python download_epa_cleanups.py

    # Just one
    python download_epa_cleanups.py --dataset superfund

    # Custom endpoint for one dataset
    python download_epa_cleanups.py --dataset brownfields --url <new-arcgis-url>

    # Offline mode
    python download_epa_cleanups.py --use-mock
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import epa_cleanups  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dataset",
        action="append",
        choices=list(epa_cleanups.DATASET_CHOICES),
        help="Which EPA dataset to fetch. Repeatable. Default: all.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help=(
            "Upstream URL override. Only valid when --dataset is passed exactly once "
            "(so the URL refers to a specific dataset)."
        ),
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=epa_cleanups.DEFAULT_MAX_AGE_HOURS,
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

    datasets = args.dataset or list(epa_cleanups.DATASET_CHOICES)
    if args.url and len(datasets) != 1:
        parser.error("--url requires exactly one --dataset")

    failures: list[str] = []
    for dataset in datasets:
        try:
            res = epa_cleanups.download(
                dataset,
                url=args.url if len(datasets) == 1 else None,
                force=args.force,
                max_age_hours=args.max_age_hours,
                use_mock=args.use_mock,
            )
        except Exception as exc:
            print(f"error: {dataset}: {exc}", file=sys.stderr)
            failures.append(dataset)
            continue
        status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
        print(
            f"[{status}] epa-cleanups/{res.relative_path}  "
            f"{res.feature_count:,} features  {res.size_bytes:,}B  "
            f"sha256={res.sha256[:12]}…  {res.absolute_path}"
        )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
