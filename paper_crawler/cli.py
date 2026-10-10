from __future__ import annotations

import argparse
import logging
import sys
import math

from .pipeline import run_from_file


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("max-matches must be a nonnegative integer") from None
    if number < 0:
        raise argparse.ArgumentTypeError("max-matches must be a nonnegative integer")
    return number


def _seconds(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("seconds must be finite and nonnegative") from None
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("seconds must be finite and nonnegative")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Crawl conference papers into Zotero.")
    parser.add_argument("--config", default="config.json", help="Path to JSON configuration.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser.add_argument("--max-matches", type=_limit, default=None,
                        help="Maximum pending matching papers to attempt across all sources (0 = unlimited). Overrides config.")
    parser.add_argument("--max-papers", type=_limit, default=None, help="Global scanned candidate limit, including detail failures (0 = unlimited).")
    parser.add_argument("--max-seconds", type=_seconds, default=None, help="Cooperative runtime deadline in seconds (0 = unlimited).")
    parser.add_argument("--scheduling", choices=["round_robin", "sequential"], default=None)
    parser.add_argument("--source-batch-size", type=_limit, default=None, help="Candidates per source/year turn; must be positive.")
    parser.add_argument("--metadata-only", action="store_true", default=None, help="Collect and filter metadata without downloading or importing.")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        overrides = {key: getattr(args, key) for key in ("max_matches", "max_papers", "max_seconds", "scheduling", "source_batch_size", "metadata_only") if getattr(args, key) is not None}
        stats = run_from_file(args.config, **overrides)
    except Exception as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1
    print(stats)
    return 130 if stats.get("interrupted") else 2 if stats["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
