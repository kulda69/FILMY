"""CLI inspekce dostupneho Plex serveru a jeho sekci."""

from __future__ import annotations

from filmy.integrations.plex import _debug_dump_json, inspect_plex_state


def main() -> int:
    """Spusti read-only inspekci Plex zdroje."""

    print(_debug_dump_json(inspect_plex_state()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
