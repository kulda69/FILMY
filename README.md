# FILMY

Osobni webova appka pro filmy a serialy postavena na `FastAPI + Jinja2 + PostgreSQL`.

## Spusteni lokalne

Spoustet z rootu projektu:

```bash
uv sync
uv run python main.py
```

Aplikace pak bezi na:

```text
http://127.0.0.1:8019
```

Kdyz uz mas pripravene lokalni virtualni prostredi, jde i kratka varianta:

```bash
.venv/bin/python main.py
```

Na Mac mini, ktery bude slouzit jako domaci server, se appka bude spoustet pres `main.py`, protoze pobehzi pod `LaunchAgents` z cesty `/Users/kulda/apps/FILMY`.

Krátký postup instalace nebo aktualizace projektu na jiném počítači je v
[`INSTALACE.md`](INSTALACE.md).

## Navrh workflow a budouci manual

Pro rozpracovany navrh vztahu mezi seznamy, akci `Watched`, `Move to`, `Copy to`
a zachovani kontextu pri praci s jednim titulem slouzi:

- [`LIST_ACTIONS_AND_TITLE_SESSION.md`](LIST_ACTIONS_AND_TITLE_SESSION.md)
- [`LIST_ACTIONS_SCENARIO_MATRIX.md`](LIST_ACTIONS_SCENARIO_MATRIX.md)
- [`LIST_ACTION_RULE_BUILDER_DRAFT.md`](LIST_ACTION_RULE_BUILDER_DRAFT.md)
- [`LIST_ACTION_DB_SCHEMA_DRAFT.md`](LIST_ACTION_DB_SCHEMA_DRAFT.md)
- [`MANUAL_TITLE_WORKFLOW_DRAFT.md`](MANUAL_TITLE_WORKFLOW_DRAFT.md)
- [`AVAILABILITY_SIGNALS_DRAFT.md`](AVAILABILITY_SIGNALS_DRAFT.md)
- [`CSFD_INTEGRATION_DRAFT.md`](CSFD_INTEGRATION_DRAFT.md)

## Poznamka k TMDB

Appka se umi spustit i bez TMDB, ale funkce zavisle na TMDB potrebuji v `.env`:

```text
TMDB_API_READ_ACCESS_TOKEN=...
```

## PostgreSQL runtime zrcadlo

Malá zapisovaná runtime vrstva je připravená také v PostgreSQL, ale aplikace
běží nad PostgreSQL. Postup správy schématu je v
[`POSTGRESQL_RUNTIME_MIGRATION.md`](POSTGRESQL_RUNTIME_MIGRATION.md).
