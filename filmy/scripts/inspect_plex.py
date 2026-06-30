from __future__ import annotations

from filmy.integrations.plex import _debug_dump_json, inspect_plex_state


def main() -> int:
    print(_debug_dump_json(inspect_plex_state()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
