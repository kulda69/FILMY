# FILMY

Osobni webova appka pro filmy a serialy postavena na `FastAPI + Jinja2 + DuckDB`.

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

Na Mac mini, ktery bude slouzit jako domaci server, se appka bude spoustet pres `main.py`, protoze pobehzi pod `LaunchAgents`.

## Poznamka k TMDB

Appka se umi spustit i bez TMDB, ale funkce zavisle na TMDB potrebuji v `.env`:

```text
TMDB_API_READ_ACCESS_TOKEN=...
```

## PostgreSQL runtime zrcadlo

Malá zapisovaná runtime vrstva je připravená také v PostgreSQL, ale aplikace
zatím zůstává na DuckDB. Postup opakované migrace, kontroly a rollbacku je v
[`POSTGRESQL_RUNTIME_MIGRATION.md`](POSTGRESQL_RUNTIME_MIGRATION.md).
