"""Vytvoří a ověří základní PostgreSQL prostor pro projekt FILMY.

Runner používá systémový klient ``psql`` a administratorské přihlášení načítá
výhradně z lokálního ``.env``. Heslo nikdy nevkládá do argumentu příkazové
řádky ani ho nevypisuje do výstupu.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import stat

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = PROJECT_ROOT / "migrations" / "postgresql"
ENV_FILE = PROJECT_ROOT / ".env"
TARGET_DATABASE = "filmy"
CONNECT_TIMEOUT_SECONDS = "10"
DEFAULT_PSQL_PATHS = (
    Path("/Library/PostgreSQL/14/bin/psql"),
    Path("/Applications/pgAdmin 4.app/Contents/SharedSupport/psql"),
)


def _assert_secure_env_file(path: Path | None = None) -> None:
    """Odmítne tajemství v odkazu, cizím souboru nebo souboru širším než 0600."""

    path = path or ENV_FILE
    try:
        metadata = path.lstat()
    except FileNotFoundError as error:
        raise RuntimeError(f"Konfigurační soubor neexistuje: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"Konfigurační soubor musí být běžný soubor, ne odkaz: {path}")
    if metadata.st_uid != os.getuid():
        raise RuntimeError(f"Konfigurační soubor nevlastní aktuální uživatel: {path}")
    if stat.S_IMODE(metadata.st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError(f"Konfigurační soubor má příliš široká práva (vyžadováno 0600): {path}")


@dataclass(frozen=True)
class PostgreSQLConfig:
    """Administrátorské připojení a nalezený klient bez zveřejnění hesla."""

    psql: Path
    host: str
    port: str
    user: str
    admin_database: str
    password: str

    def subprocess_environment(self) -> dict[str, str]:
        """Vrátí minimální prostředí; cizí PG* nastavení se nepřenáší."""

        return {
            "PGPASSWORD": self.password,
            "PGCONNECT_TIMEOUT": CONNECT_TIMEOUT_SECONDS,
            "LC_ALL": "C",
            "LANG": "C",
        }


def _resolve_psql(explicit_path: str | None) -> Path:
    """Najde spustitelný ``psql`` ve stabilním, zdokumentovaném pořadí."""

    if explicit_path:
        candidate = Path(explicit_path).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
        raise RuntimeError(
            "POSTGRES_PSQL_PATH ukazuje na neexistující nebo nespustitelný "
            f"soubor: {candidate}"
        )

    candidates = list(DEFAULT_PSQL_PATHS)

    path_psql = shutil.which("psql")
    if path_psql:
        candidates.append(Path(path_psql))

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()

    searched = ", ".join(str(path) for path in candidates) or "žádná cesta"
    raise RuntimeError(
        "Klient psql nebyl nalezen nebo není spustitelný. "
        f"Zkontrolované cesty: {searched}. "
        "Případně nastav POSTGRES_PSQL_PATH v .env."
    )


def _load_config() -> PostgreSQLConfig:
    """Načte autoritativní konfiguraci jen z projektového ``.env``."""

    _assert_secure_env_file()

    values = dotenv_values(ENV_FILE, interpolate=False)
    password = values.get("POSTGRES_ADMIN_PASSWORD") or ""
    if not password:
        raise RuntimeError("POSTGRES_ADMIN_PASSWORD v .env chybí nebo je prázdné.")

    return PostgreSQLConfig(
        psql=_resolve_psql(values.get("POSTGRES_PSQL_PATH")),
        host=values.get("POSTGRES_ADMIN_HOST") or "/private/tmp",
        port=values.get("POSTGRES_ADMIN_PORT") or "5432",
        user=values.get("POSTGRES_ADMIN_USER") or "postgres",
        admin_database=values.get("POSTGRES_ADMIN_DATABASE") or "postgres",
        password=password,
    )


def _run_psql(
    config: PostgreSQLConfig,
    database: str,
    *arguments: str,
    capture_output: bool = False,
) -> str:
    """Spustí ``psql`` se stabilními volbami a bez zděděného PG prostředí."""

    command = [
        str(config.psql),
        "-X",
        "-v",
        "ON_ERROR_STOP=1",
        "-P",
        "pager=off",
        "-h",
        config.host,
        "-p",
        config.port,
        "-U",
        config.user,
        "-d",
        database,
        *arguments,
    ]
    completed = subprocess.run(
        command,
        env=config.subprocess_environment(),
        check=True,
        capture_output=True,
        text=True,
    )
    if capture_output:
        return completed.stdout

    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    return ""


def _sanitized_stderr(error: subprocess.CalledProcessError, password: str) -> str:
    """Vrátí stručný diagnostický výstup ``psql`` bez administratorského hesla."""

    stderr = error.stderr if isinstance(error.stderr, str) else ""
    sanitized = stderr.replace(password, "[REDACTED]") if password else stderr
    lines = [line.strip() for line in sanitized.splitlines() if line.strip()]
    summary = " | ".join(lines[-6:])
    if len(summary) > 1200:
        summary = "…" + summary[-1199:]
    return summary


def bootstrap(config: PostgreSQLConfig) -> None:
    """Vytvoří databázi a její základní schémata, rozšíření a ACL."""

    _run_psql(
        config,
        config.admin_database,
        "-f",
        str(MIGRATIONS_DIR / "000_create_database.sql"),
    )
    _run_psql(
        config,
        TARGET_DATABASE,
        "-f",
        str(MIGRATIONS_DIR / "001_bootstrap.sql"),
    )


def check(config: PostgreSQLConfig) -> None:
    """Fail-closed ověří vlastnictví, umístění rozšíření a přesné ACL."""

    query = """
        WITH protected_schemas(name) AS (
            VALUES ('public'), ('app'), ('old')
        ), protected_extensions(name) AS (
            VALUES ('pg_trgm'), ('unaccent'), ('fuzzystrmatch')
        ), violations AS (
            SELECT format('database=%s (expected filmy)', current_database()) AS detail
            WHERE current_database() <> 'filmy'
            UNION ALL
            SELECT format('database owner=%s (expected %s)', pg_get_userbyid(datdba), current_user)
            FROM pg_database
            WHERE datname = current_database() AND datdba <> current_user::regrole
            UNION ALL
            SELECT format('schema %s missing or owner=%s (expected %s)', expected.name,
                          COALESCE(pg_get_userbyid(namespace.nspowner), '<missing>'), current_user)
            FROM protected_schemas AS expected
            LEFT JOIN pg_namespace AS namespace ON namespace.nspname = expected.name
            WHERE namespace.oid IS NULL OR namespace.nspowner <> current_user::regrole
            UNION ALL
            SELECT format('extension %s missing or owner=%s schema=%s (expected %s/public)',
                          expected.name, COALESCE(pg_get_userbyid(extension_entry.extowner), '<missing>'),
                          COALESCE(namespace.nspname, '<missing>'), current_user)
            FROM protected_extensions AS expected
            LEFT JOIN pg_extension AS extension_entry ON extension_entry.extname = expected.name
            LEFT JOIN pg_namespace AS namespace ON namespace.oid = extension_entry.extnamespace
            WHERE extension_entry.oid IS NULL
               OR extension_entry.extowner <> current_user::regrole
               OR namespace.nspname <> 'public'
            UNION ALL
            SELECT format('database ACL %s:%s is not allowed',
                          COALESCE(pg_get_userbyid(privilege.grantee), 'PUBLIC'), privilege.privilege_type)
            FROM pg_database AS database_entry,
                 LATERAL aclexplode(COALESCE(database_entry.datacl, '{}'::aclitem[])) AS privilege
            WHERE database_entry.datname = current_database()
              AND privilege.grantee <> database_entry.datdba
              AND NOT (
                  pg_get_userbyid(privilege.grantee) = 'filmy_app'
                  AND privilege.privilege_type = 'CONNECT'
              )
            UNION ALL
            SELECT format('schema ACL %s.%s:%s is not allowed', schema_entry.nspname,
                          COALESCE(pg_get_userbyid(privilege.grantee), 'PUBLIC'), privilege.privilege_type)
            FROM pg_namespace AS schema_entry,
                 LATERAL aclexplode(COALESCE(schema_entry.nspacl, '{}'::aclitem[])) AS privilege
            WHERE schema_entry.nspname IN ('public', 'app', 'old')
              AND privilege.grantee <> schema_entry.nspowner
              AND NOT (
                  schema_entry.nspname = 'public'
                  AND privilege.grantee = 0
                  AND privilege.privilege_type = 'USAGE'
              )
              AND NOT (
                  schema_entry.nspname = 'app'
                  AND pg_get_userbyid(privilege.grantee) = 'filmy_app'
                  AND privilege.privilege_type = 'USAGE'
              )
              AND NOT (
                  schema_entry.nspname = 'old'
                  AND pg_get_userbyid(privilege.grantee) = 'filmy_app'
                  AND privilege.privilege_type = 'USAGE'
              )
        )
        SELECT concat_ws(E'\\t', '0', 'IDENTITY', current_user)
        UNION ALL
        SELECT concat_ws(E'\\t', '1', 'VIOLATION', detail)
        FROM violations
        ORDER BY 1;
    """
    output = _run_psql(
        config,
        TARGET_DATABASE,
        "-A",
        "-t",
        "-c",
        query,
        capture_output=True,
    ).strip()
    lines = [line for line in output.splitlines() if line.strip()]
    identity = lines[0].split("\t", 2) if lines else []
    failures = [
        line.split("\t", 2)[2]
        for line in lines[1:]
        if line.startswith("1\tVIOLATION\t")
    ]
    if identity != ["0", "IDENTITY", config.user]:
        actual_user = identity[2] if len(identity) == 3 else "chybí"
        failures.insert(
            0,
            f"connected_user={actual_user} (očekáváno {config.user})",
        )
    if failures:
        raise RuntimeError("Kontrola PostgreSQL selhala: " + "; ".join(failures))

    print(
        "PostgreSQL kontrola OK: vlastníci filmy/app/old/public, "
        "pg_trgm/unaccent/fuzzystrmatch v public a přesné ACL včetně filmy_app."
    )


def main() -> None:
    """Zpracuje CLI volby a vypíše stručnou chybu bez tracebacku."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Pouze ověří databázi, schémata, rozšíření a přesné ACL.",
    )
    arguments = parser.parse_args()

    try:
        config = _load_config()
        if arguments.check:
            check(config)
            return
        bootstrap(config)
        check(config)
    except subprocess.CalledProcessError as error:
        detail = _sanitized_stderr(error, config.password)
        message = (
            f"Chyba PostgreSQL bootstrapu: psql skončil s kódem {error.returncode}."
        )
        if detail:
            message += f" {detail}"
        print(message, file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Chyba PostgreSQL bootstrapu: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
