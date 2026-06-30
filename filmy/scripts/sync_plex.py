from __future__ import annotations

import json

from filmy.db import ensure_database, sync_plex_source


def main() -> int:
    ensure_database()
    result = sync_plex_source()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
