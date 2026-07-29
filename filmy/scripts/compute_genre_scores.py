"""CLI vstup pro vypocet a ulozeni zanroveho scoringu."""

from __future__ import annotations

import argparse
import json

from filmy.db import compute_and_record_genre_scores


def main() -> int:
    """Spusti vypocet genre scoringu a vrati shell exit code."""

    parser = argparse.ArgumentParser(description="Compute one local genre score snapshot.")
    parser.add_argument("--scope", default="default", help="Logical score scope label.")
    parser.add_argument("--source-ref", default="script.compute_genre_scores", help="Source reference stored with the snapshot.")
    args = parser.parse_args()

    result = compute_and_record_genre_scores(
        score_scope=args.scope,
        source_origin="local_script",
        source_ref=args.source_ref,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
