from __future__ import annotations

import json
from pathlib import Path

from filmy.db import _title_detail_cache_path, clear_title_presentation_cache, get_title_presentation
from filmy.paths import ASSETS_DIR


def _iter_stale_title_tconsts() -> list[str]:
    items: list[str] = []
    for path in ASSETS_DIR.glob("*/detail.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            items.append(path.parent.name)
            continue

        kind = data.get("kind")
        has_poster = data.get("has_poster")
        has_backdrop = data.get("has_backdrop")
        poster_url = data.get("poster_url")
        if kind not in {"title", "episode"}:
            items.append(path.parent.name)
            continue
        if has_poster is None or has_backdrop is None:
            items.append(path.parent.name)
            continue
        if has_poster and not poster_url:
            items.append(path.parent.name)
            continue

    return sorted(set(items))


def main() -> int:
    targets = _iter_stale_title_tconsts()
    print(json.dumps({"phase": "start", "targets": len(targets)}, ensure_ascii=False), flush=True)

    clear_title_presentation_cache()
    rewritten = 0
    missing = 0
    for index, tconst in enumerate(targets, start=1):
        cache_path = _title_detail_cache_path(tconst)
        cache_path.unlink(missing_ok=True)
        item = get_title_presentation(tconst)
        if item is None:
            missing += 1
        else:
            rewritten += 1

        if index % 50 == 0 or index == len(targets):
            print(
                json.dumps(
                    {
                        "phase": "progress",
                        "index": index,
                        "targets": len(targets),
                        "rewritten": rewritten,
                        "missing": missing,
                        "tconst": tconst,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    print(
        json.dumps(
            {
                "phase": "done",
                "targets": len(targets),
                "rewritten": rewritten,
                "missing": missing,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
