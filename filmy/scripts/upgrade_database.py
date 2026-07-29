"""Apply PostgreSQL database upgrades after a project update.

The runner is intentionally small and conservative: it uses the same admin
configuration and ``psql`` execution path as ``bootstrap_postgresql.py``, keeps a
ledger in ``app.database_upgrades`` and applies only idempotent SQL files.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import sys

from filmy.scripts.bootstrap_postgresql import (
    MIGRATIONS_DIR,
    TARGET_DATABASE,
    _load_config,
    _run_psql,
    _sanitized_stderr,
)


UPGRADES: tuple[tuple[str, str], ...] = (
    ("0002-runtime-schema", "002_runtime_schema.sql"),
    ("0003-runtime-grants", "003_runtime_grants.sql"),
    ("0004-catalog-schema", "004_catalog_schema.sql"),
    ("0005-catalog-grants", "005_catalog_grants.sql"),
    ("0006-list-actions-session-schema", "006_list_actions_session_schema.sql"),
    ("0007-list-actions-session-grants", "007_list_actions_session_grants.sql"),
    ("0008-list-action-rule-seed", "008_list_action_rule_seed.sql"),
    ("0009-list-action-target-rule-seed", "009_list_action_target_rule_seed.sql"),
)


def _sql_literal(value: str) -> str:
    """Return a single-quoted SQL literal for internal script metadata."""

    return "'" + value.replace("'", "''") + "'"


def _sha256(path: Path) -> str:
    """Hash the migration file so changed upgrade contents are visible."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_ledger(config) -> None:
    """Create the upgrade ledger after the app schema is available."""

    _run_psql(
        config,
        TARGET_DATABASE,
        "-c",
        """
        CREATE TABLE IF NOT EXISTS app.database_upgrades (
            version text PRIMARY KEY,
            script_name text NOT NULL,
            checksum_sha256 text NOT NULL,
            applied_at timestamp without time zone NOT NULL DEFAULT now()
        );
        """,
    )


def _applied_versions(config) -> set[str]:
    """Read versions already recorded in the upgrade ledger."""

    output = _run_psql(
        config,
        TARGET_DATABASE,
        "-A",
        "-t",
        "-c",
        "SELECT version FROM app.database_upgrades ORDER BY version;",
        capture_output=True,
    )
    return {line.strip() for line in output.splitlines() if line.strip()}


def _record_upgrade(config, version: str, script_name: str, checksum: str) -> None:
    """Record a successfully applied upgrade version."""

    query = (
        "INSERT INTO app.database_upgrades (version, script_name, checksum_sha256) "
        f"VALUES ({_sql_literal(version)}, {_sql_literal(script_name)}, {_sql_literal(checksum)}) "
        "ON CONFLICT (version) DO UPDATE SET "
        "script_name = excluded.script_name, "
        "checksum_sha256 = excluded.checksum_sha256;"
    )
    _run_psql(config, TARGET_DATABASE, "-c", query)


def _bootstrap_required(config) -> bool:
    """Return True only when the core bootstrap schema is still missing."""

    output = _run_psql(
        config,
        TARGET_DATABASE,
        "-A",
        "-t",
        "-c",
        "SELECT to_regnamespace('app') IS NOT NULL;",
        capture_output=True,
    )
    return output.strip().lower() not in {"t", "true"}


def upgrade_database(*, dry_run: bool = False) -> None:
    """Apply pending database upgrades in version order."""

    # 001 creates app/raw/old/public foundations and is safe to run repeatedly.
    bootstrap_path = MIGRATIONS_DIR / "001_bootstrap.sql"
    if dry_run:
        print(f"would apply foundation: {bootstrap_path}")
        for version, script_name in UPGRADES:
            path = MIGRATIONS_DIR / script_name
            print(f"would apply {version} {script_name} {_sha256(path)}")
        return

    config = _load_config()
    if _bootstrap_required(config):
        print(f"apply 0001-bootstrap {bootstrap_path.name}")
        _run_psql(config, TARGET_DATABASE, "-f", str(bootstrap_path))
    else:
        print(f"skip 0001-bootstrap {bootstrap_path.name} (app schema already exists)")
    _ensure_ledger(config)
    _record_upgrade(config, "0001-bootstrap", bootstrap_path.name, _sha256(bootstrap_path))
    applied = _applied_versions(config)

    for version, script_name in UPGRADES:
        path = MIGRATIONS_DIR / script_name
        checksum = _sha256(path)
        if version in applied:
            print(f"skip {version} {script_name}")
            continue
        print(f"apply {version} {script_name}")
        _run_psql(config, TARGET_DATABASE, "-f", str(path))
        _record_upgrade(config, version, script_name, checksum)

    print("Database upgrade OK.")


def main() -> None:
    """CLI entrypoint for deployment-time database upgrades."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List upgrade files without applying them.",
    )
    arguments = parser.parse_args()

    try:
        upgrade_database(dry_run=arguments.dry_run)
    except subprocess.CalledProcessError as error:
        password = ""
        try:
            password = _load_config().password
        except Exception:
            pass
        detail = _sanitized_stderr(error, password)
        message = f"Chyba databazoveho upgradu: psql skoncil s kodem {error.returncode}."
        if detail:
            message += f" {detail}"
        print(message, file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Chyba databazoveho upgradu: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
