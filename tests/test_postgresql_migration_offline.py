"""Offline regresní testy PostgreSQL runneru; nikdy se nepřipojují k serveru."""

from __future__ import annotations

from pathlib import Path
import os
from subprocess import CompletedProcess
import sys
import tempfile
import unittest
from unittest.mock import patch

from filmy.scripts import migrate_runtime_to_postgresql as migration
from filmy.scripts import bootstrap_postgresql as bootstrap


def _config(user: str = "postgres", password: str = "secret") -> migration.ConnectionConfig:
    return migration.ConnectionConfig(Path("/fake/psql"), "/tmp", "5432", "postgres", user, password)


class RuntimeMigrationOfflineTests(unittest.TestCase):
    def test_fake_bootstrap_baseline_does_not_allow_public_temporary(self) -> None:
        captured: list[str] = []

        def fake_run(config, database, *arguments, **kwargs):
            captured.append(arguments[-1])
            return "0\tIDENTITY\tpostgres\n"

        config = bootstrap.PostgreSQLConfig(
            Path("/fake/psql"), "/tmp", "5432", "postgres", "postgres", "secret"
        )
        with patch.object(bootstrap, "_run_psql", side_effect=fake_run):
            bootstrap.check(config)
        self.assertNotIn("privilege_type = 'TEMPORARY'", captured[0])
        sql_001 = (bootstrap.MIGRATIONS_DIR / "001_bootstrap.sql").read_text(encoding="utf-8")
        self.assertIn("REVOKE TEMPORARY ON DATABASE filmy FROM PUBLIC", sql_001)

    def test_schema_fingerprint_catalog_covers_drift_classes(self) -> None:
        sql = migration._schema_verification_sql()
        for marker in (
            "relpersistence", "relrowsecurity", "relforcerowsecurity", "pg_policy",
            "tgisinternal", "condeferrable", "condeferred", "convalidated",
            "indisvalid", "indisready", "pg_opclass", "pg_collation", "indoption",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, sql)

    def test_schema_fingerprint_rejects_every_fake_drift_line(self) -> None:
        for drift in (
            "table security/persistence drift: user_lists",
            "unexpected row security policy: user_lists.bad",
            "unexpected trigger: user_lists.bad",
            "constraint missing/extra/drift: deferrable",
            "index missing/extra/drift: invalid",
        ):
            with self.subTest(drift=drift), patch.object(
                migration, "_psql", return_value=CompletedProcess([], 0, drift + "\n", "")
            ):
                with self.assertRaisesRegex(RuntimeError, "runtime schéma nesedí"):
                    migration.verify_schema_fingerprint(_config())

    def test_exact_acl_accepts_empty_violation_stream(self) -> None:
        responses = iter((
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 1, "", "forbidden"),
            CompletedProcess([], 1, "", "forbidden"),
            CompletedProcess([], 1, "", "forbidden"),
            CompletedProcess([], 0, "0\n0\n0\n", ""),
        ))

        def fake_psql(*args, **kwargs):
            return next(responses)

        with patch.object(migration, "_psql", side_effect=fake_psql):
            migration.verify_role(_config(), _config("filmy_app", "app-secret"))

    def test_exact_acl_rejects_public_or_other_role(self) -> None:
        for grantee in ("PUBLIC", "other_role"):
            line = f'"table ACL missing/extra: user_lists:{grantee}:SELECT"\n'
            with self.subTest(grantee=grantee), patch.object(
                migration, "_psql", return_value=CompletedProcess([], 0, line, "")
            ):
                with self.assertRaisesRegex(RuntimeError, "oprávnění role"):
                    migration.verify_role(_config(), _config("filmy_app", "app-secret"))

    def test_full_row_samples_use_every_exported_column(self) -> None:
        self.assertEqual(migration.SAMPLE_COLUMNS, migration.TABLE_COLUMNS)
        with tempfile.TemporaryDirectory() as directory:
            snapshot = migration.export_source(Path(directory))
        self.assertEqual(sum(snapshot.counts.values()), 22_904)
        for table, row in snapshot.samples.items():
            self.assertEqual(len(row), len(migration.TABLE_COLUMNS[table]))

    def test_sanitizer_redacts_raw_and_sql_escaped_apostrophe_secret(self) -> None:
        secret = "abc'def"
        text = f"raw={secret}; escaped='abc''def'"
        sanitized = migration._sanitize_text(text, (secret,))
        self.assertNotIn("abc", sanitized)
        self.assertNotIn("def", sanitized)
        self.assertGreaterEqual(sanitized.count("[REDACTED]"), 2)

    def test_app_env_write_is_atomic_parseable_and_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text('POSTGRES_ADMIN_PASSWORD="admin"\nKEEP="yes"\n', encoding="utf-8")
            env_file.chmod(0o600)
            with patch.object(migration, "ENV_FILE", env_file):
                migration._write_app_config("url_safe-'secret", _config())
            values = migration.dotenv_values(env_file, interpolate=False)
            self.assertEqual(values["POSTGRES_APP_PASSWORD"], "url_safe-'secret")
            self.assertEqual(values["KEEP"], "yes")
            self.assertEqual(os.stat(env_file).st_mode & 0o777, 0o600)

    def test_import_sql_is_one_transaction_and_delete_follows_staging(self) -> None:
        captured: list[str] = []

        def fake_psql(*args, **kwargs):
            if kwargs.get("sql"):
                captured.append(kwargs["sql"])
            return CompletedProcess([], 0, "", "")

        with tempfile.TemporaryDirectory() as directory, patch.object(
            migration, "verify_schema_fingerprint"
        ), patch.object(migration, "_psql", side_effect=fake_psql):
            migration.import_snapshot(_config(), Path(directory))
        sql = captured[-1]
        self.assertTrue(sql.startswith("BEGIN;"))
        self.assertTrue(sql.rstrip().endswith("COMMIT;"))
        self.assertLess(sql.index("CREATE TEMP TABLE"), sql.index("DELETE FROM"))
        self.assertLess(sql.index("DELETE FROM"), sql.index("INSERT INTO app."))

    def test_schema_apply_verifies_existing_schema_before_002_and_grants_after_fingerprint(self) -> None:
        events: list[str] = []

        def fake_psql(*args, **kwargs):
            file = kwargs.get("file")
            events.append(file.name if file else "inspect-query")
            return CompletedProcess([], 0, "", "")

        with patch.object(
            migration, "inspect_target_schema_before_apply",
            side_effect=lambda config: events.append("preflight") or "existing",
        ), patch.object(
            migration, "verify_schema_fingerprint",
            side_effect=lambda config: events.append("fingerprint"),
        ), patch.object(migration, "_psql", side_effect=fake_psql):
            migration.apply_schema(_config())
        self.assertEqual(
            events,
            ["preflight", "002_runtime_schema.sql", "fingerprint", "003_runtime_grants.sql"],
        )

    def test_schema_preflight_allows_only_empty_or_exact_six_tables(self) -> None:
        with patch.object(
            migration, "_psql", return_value=CompletedProcess([], 0, "", "")
        ):
            self.assertEqual(migration.inspect_target_schema_before_apply(_config()), "fresh")

        exact = "\n".join(f"{table},r" for table in migration.TABLE_COLUMNS) + "\nidx,i\n"
        with patch.object(
            migration, "_psql", return_value=CompletedProcess([], 0, exact, "")
        ), patch.object(migration, "verify_schema_fingerprint") as fingerprint:
            self.assertEqual(migration.inspect_target_schema_before_apply(_config()), "existing")
            fingerprint.assert_called_once()

        partial = "user_lists,r\n"
        with patch.object(
            migration, "_psql", return_value=CompletedProcess([], 0, partial, "")
        ), self.assertRaisesRegex(RuntimeError, "částečné"):
            migration.inspect_target_schema_before_apply(_config())

    def test_002_contains_no_app_acl_and_003_is_transactional(self) -> None:
        schema_sql = migration.SCHEMA_MIGRATION.read_text(encoding="utf-8")
        grants_sql = migration.GRANTS_MIGRATION.read_text(encoding="utf-8")
        self.assertNotIn("GRANT ", schema_sql)
        self.assertNotIn("REVOKE ", schema_sql)
        self.assertTrue(grants_sql.lstrip().startswith("--"))
        self.assertIn("BEGIN;", grants_sql)
        self.assertTrue(grants_sql.rstrip().endswith("COMMIT;"))
        self.assertIn("REVOKE ALL PRIVILEGES", grants_sql)
        self.assertIn("GRANT SELECT, INSERT, UPDATE, DELETE", grants_sql)
        self.assertNotIn("DEFAULT PRIVILEGES", grants_sql)

    def test_main_preflight_role_runs_before_export_and_import(self) -> None:
        events: list[str] = []
        admin = _config()
        app = _config("filmy_app", "url-safe-app-secret")
        snapshot = migration.SourceSnapshot({}, {})

        def event(name, result=None):
            def callback(*args, **kwargs):
                events.append(name)
                return result
            return callback

        patches = (
            patch.object(migration, "_load_values", return_value={}),
            patch.object(migration, "_config", return_value=admin),
            patch.object(migration, "verify_bootstrap_baseline", side_effect=event("bootstrap")),
            patch.object(migration, "_validated_app_secret", return_value="url-safe-app-secret"),
            patch.object(migration, "provision_role", side_effect=event("provision", app)),
            patch.object(migration, "apply_schema", side_effect=event("schema")),
            patch.object(migration, "verify_role", side_effect=event("role")),
            patch.object(migration, "export_source", side_effect=event("export", snapshot)),
            patch.object(migration, "import_snapshot", side_effect=event("import")),
            patch.object(migration, "verify_data", side_effect=event("data")),
            patch.object(migration, "verify_schema_fingerprint", side_effect=event("fingerprint")),
            patch.object(sys, "argv", ["migrate-runtime"]),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             patches[6], patches[7], patches[8], patches[9], patches[10], patches[11]:
            migration.main()
        self.assertEqual(
            events,
            ["bootstrap", "provision", "schema", "role", "export", "import", "data", "fingerprint", "role"],
        )


if __name__ == "__main__":
    unittest.main()
