from __future__ import annotations

from filmy.integrations.plex import _debug_dump_json, build_import_probe
from filmy.paths import DATA_DIR


OUTPUT_PATH = DATA_DIR / "plex_probe.json"


def main() -> int:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    probe = build_import_probe()
    OUTPUT_PATH.write_text(_debug_dump_json(probe) + "\n", encoding="utf-8")
    print(OUTPUT_PATH.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
