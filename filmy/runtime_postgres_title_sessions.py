"""Title-session storage a orchestrace pro PostgreSQL runtime FILMY."""

from __future__ import annotations

from typing import Any, Callable
import json
import uuid


class TitleSessionStore:
    """Zapouzdrena storage vrstva pro list-action pravidla a title session.

    Tahle mini-domena ma vlastni sadu tabulek a jeden zretelny workflow:
    nacist pravidla, zalozit nebo obnovit session, zapisovat uzivatelske akce
    a pripravovat effect queue. Drzet to jako jednu tridu je citelnejsi nez
    dalsi davka nesouvisejicich volnych funkci po celem modulu.
    """

    def __init__(
        self,
        *,
        connect: Callable[[], Any],
        loads_json_or_default: Callable[..., Any],
        parse_optional_timestamp: Callable[[Any], Any],
    ) -> None:
        """Uloz zavislosti potrebne pro storage operace bez primeho import cyklu."""

        self._connect = connect
        self._loads_json_or_default = loads_json_or_default
        self._parse_optional_timestamp = parse_optional_timestamp

    def fetch_rules(
        self,
        *,
        source_list_id: str,
        trigger_action: str | None = None,
        target_list_id: str | None = None,
        enabled_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Nacti pravidla pro konkretni zdrojovy list a volitelny trigger."""

        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    rule_id,
                    source_list_id,
                    trigger_action,
                    target_list_id,
                    effect_type,
                    phase,
                    order_index,
                    enabled,
                    lock_reason_key,
                    lock_reason_text,
                    effect_params,
                    created_at,
                    updated_at
                FROM app.list_action_rules
                WHERE source_list_id = %s
                  AND (%s::text IS NULL OR trigger_action = %s::text)
                  AND (%s::text IS NULL OR target_list_id = %s::text)
                  AND (%s::boolean = FALSE OR enabled = TRUE)
                ORDER BY trigger_action, target_list_id NULLS FIRST, phase, order_index, rule_id
                """,
                (source_list_id, trigger_action, trigger_action, target_list_id, target_list_id, enabled_only),
            )
            rows = cursor.fetchall()
        return [self._row_to_list_action_rule(row) for row in rows]

    def fetch_rule(self, rule_id: str) -> dict[str, Any] | None:
        """Nacti jedno pravidlo podle technickeho id."""

        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    rule_id,
                    source_list_id,
                    trigger_action,
                    target_list_id,
                    effect_type,
                    phase,
                    order_index,
                    enabled,
                    lock_reason_key,
                    lock_reason_text,
                    effect_params,
                    created_at,
                    updated_at
                FROM app.list_action_rules
                WHERE rule_id = %s
                """,
                (rule_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_list_action_rule(row)

    def upsert_rule(
        self,
        *,
        rule_id: str,
        source_list_id: str,
        trigger_action: str,
        target_list_id: str | None,
        effect_type: str,
        phase: str,
        order_index: int,
        enabled: bool,
        lock_reason_key: str | None,
        lock_reason_text: str | None,
        effect_params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Vloz nebo uprav jedno list-action pravidlo."""

        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO app.list_action_rules (
                    rule_id,
                    source_list_id,
                    trigger_action,
                    target_list_id,
                    effect_type,
                    phase,
                    order_index,
                    enabled,
                    lock_reason_key,
                    lock_reason_text,
                    effect_params
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (rule_id) DO UPDATE SET
                    source_list_id = excluded.source_list_id,
                    trigger_action = excluded.trigger_action,
                    target_list_id = excluded.target_list_id,
                    effect_type = excluded.effect_type,
                    phase = excluded.phase,
                    order_index = excluded.order_index,
                    enabled = excluded.enabled,
                    lock_reason_key = excluded.lock_reason_key,
                    lock_reason_text = excluded.lock_reason_text,
                    effect_params = excluded.effect_params
                RETURNING
                    rule_id,
                    source_list_id,
                    trigger_action,
                    target_list_id,
                    effect_type,
                    phase,
                    order_index,
                    enabled,
                    lock_reason_key,
                    lock_reason_text,
                    effect_params,
                    created_at,
                    updated_at
                """,
                (
                    rule_id,
                    source_list_id,
                    trigger_action,
                    target_list_id,
                    effect_type,
                    phase,
                    order_index,
                    enabled,
                    lock_reason_key,
                    lock_reason_text,
                    json.dumps(effect_params or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(f"PostgreSQL upsert list action rule {rule_id} nevratil vysledek.")
            conn.commit()
        return self._row_to_list_action_rule(row)

    def delete_rule(self, rule_id: str) -> bool:
        """Smaz jedno list-action pravidlo a vrat, jestli existovalo."""

        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM app.list_action_rules
                WHERE rule_id = %s
                RETURNING rule_id
                """,
                (rule_id,),
            )
            row = cursor.fetchone()
            conn.commit()
        return row is not None

    def upsert_session(
        self,
        *,
        session_id: str,
        tconst: str,
        status: str,
        opened_from: str | None,
        return_to_url: str | None,
        source_list_id: str | None,
        session_scope: str,
        started_at: str,
    ) -> dict[str, Any]:
        """Zaloz nebo obnov jednu title session a vrat ulozeny stav."""

        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO app.title_sessions (
                    session_id,
                    tconst,
                    status,
                    opened_from,
                    return_to_url,
                    source_list_id,
                    session_scope,
                    started_at,
                    updated_at,
                    finalized_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::timestamp, %s::timestamp, NULL)
                ON CONFLICT (session_id) DO UPDATE SET
                    tconst = excluded.tconst,
                    status = excluded.status,
                    opened_from = excluded.opened_from,
                    return_to_url = excluded.return_to_url,
                    source_list_id = excluded.source_list_id,
                    session_scope = excluded.session_scope,
                    updated_at = excluded.updated_at
                RETURNING
                    session_id,
                    tconst,
                    status,
                    opened_from,
                    return_to_url,
                    source_list_id,
                    session_scope,
                    started_at,
                    updated_at,
                    finalized_at
                """,
                (
                    session_id,
                    tconst,
                    status,
                    opened_from,
                    return_to_url,
                    source_list_id,
                    session_scope,
                    started_at,
                    started_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(f"PostgreSQL upsert title session {session_id} nevratil vysledek.")
            conn.commit()
        return self._row_to_title_session(row)

    def fetch_session(self, session_id: str) -> dict[str, Any] | None:
        """Nacti jednu title session podle session id."""

        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    session_id,
                    tconst,
                    status,
                    opened_from,
                    return_to_url,
                    source_list_id,
                    session_scope,
                    started_at,
                    updated_at,
                    finalized_at
                FROM app.title_sessions
                WHERE session_id = %s
                """,
                (session_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_title_session(row)

    def insert_action(
        self,
        *,
        action_id: str,
        session_id: str,
        tconst: str,
        source_list_id: str | None,
        trigger_action: str,
        target_list_id: str | None,
        rating_value: int | None,
        notes_text: str | None,
        action_payload: dict[str, Any],
        action_order: int,
        created_at: str,
    ) -> dict[str, Any]:
        """Zapis jednu explicitni akci uzivatele do title session."""

        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO app.title_session_actions (
                    action_id,
                    session_id,
                    tconst,
                    source_list_id,
                    trigger_action,
                    target_list_id,
                    rating_value,
                    notes_text,
                    action_payload,
                    action_order,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::timestamp)
                RETURNING
                    action_id,
                    session_id,
                    tconst,
                    source_list_id,
                    trigger_action,
                    target_list_id,
                    rating_value,
                    notes_text,
                    action_payload,
                    action_order,
                    created_at
                """,
                (
                    action_id,
                    session_id,
                    tconst,
                    source_list_id,
                    trigger_action,
                    target_list_id,
                    rating_value,
                    notes_text,
                    json.dumps(action_payload, ensure_ascii=False, sort_keys=True),
                    action_order,
                    created_at,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(f"PostgreSQL insert title session action {action_id} nevratil vysledek.")
            conn.commit()
        return self._row_to_title_session_action(row)

    def fetch_actions(self, session_id: str) -> list[dict[str, Any]]:
        """Nacti explicitni akce jedne title session v poradi."""

        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    action_id,
                    session_id,
                    tconst,
                    source_list_id,
                    trigger_action,
                    target_list_id,
                    rating_value,
                    notes_text,
                    action_payload,
                    action_order,
                    created_at
                FROM app.title_session_actions
                WHERE session_id = %s
                ORDER BY action_order, created_at, action_id
                """,
                (session_id,),
            )
            rows = cursor.fetchall()
        return [self._row_to_title_session_action(row) for row in rows]

    def fetch_action(self, action_id: str) -> dict[str, Any] | None:
        """Nacti jednu explicitni session akci podle id."""

        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    action_id,
                    session_id,
                    tconst,
                    source_list_id,
                    trigger_action,
                    target_list_id,
                    rating_value,
                    notes_text,
                    action_payload,
                    action_order,
                    created_at
                FROM app.title_session_actions
                WHERE action_id = %s
                """,
                (action_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_title_session_action(row)

    def insert_effect_rows(self, rows: list[dict[str, Any]]) -> None:
        """Vloz pripravenou effect queue pro jednu nebo vice session akci."""

        if not rows:
            return
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO app.title_session_effect_queue (
                    effect_id,
                    session_id,
                    action_id,
                    rule_id,
                    tconst,
                    effect_type,
                    phase,
                    source_list_id,
                    target_list_id,
                    effect_status,
                    effect_order,
                    effect_payload,
                    created_at,
                    executed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::timestamp, %s::timestamp)
                """,
                [
                    (
                        row["effect_id"],
                        row["session_id"],
                        row.get("action_id"),
                        row.get("rule_id"),
                        row["tconst"],
                        row["effect_type"],
                        row["phase"],
                        row.get("source_list_id"),
                        row.get("target_list_id"),
                        row["effect_status"],
                        row["effect_order"],
                        json.dumps(row.get("effect_payload") or {}, ensure_ascii=False, sort_keys=True),
                        row["created_at"],
                        row.get("executed_at"),
                    )
                    for row in rows
                ],
            )
            conn.commit()

    def fetch_effect_queue(
        self,
        session_id: str,
        *,
        phase: str | None = None,
        effect_status: str | None = None,
    ) -> list[dict[str, Any]]:
        """Nacti effect queue pro jednu session s volitelnym filtrem."""

        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    effect_id,
                    session_id,
                    action_id,
                    rule_id,
                    tconst,
                    effect_type,
                    phase,
                    source_list_id,
                    target_list_id,
                    effect_status,
                    effect_order,
                    effect_payload,
                    created_at,
                    executed_at
                FROM app.title_session_effect_queue
                WHERE session_id = %s
                  AND (%s::text IS NULL OR phase = %s::text)
                  AND (%s::text IS NULL OR effect_status = %s::text)
                ORDER BY effect_order, created_at, effect_id
                """,
                (session_id, phase, phase, effect_status, effect_status),
            )
            rows = cursor.fetchall()
        return [self._row_to_title_session_effect(row) for row in rows]

    def update_session_status(
        self,
        session_id: str,
        *,
        status: str,
        updated_at: str,
        finalized_at: str | None = None,
    ) -> dict[str, Any]:
        """Zmen stav title session a vrat aktualni radek."""

        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE app.title_sessions
                SET
                    status = %s,
                    updated_at = %s::timestamp,
                    finalized_at = CASE
                        WHEN %s::timestamp IS NULL THEN finalized_at
                        ELSE %s::timestamp
                    END
                WHERE session_id = %s
                RETURNING
                    session_id,
                    tconst,
                    status,
                    opened_from,
                    return_to_url,
                    source_list_id,
                    session_scope,
                    started_at,
                    updated_at,
                    finalized_at
                """,
                (status, updated_at, finalized_at, finalized_at, session_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(f"PostgreSQL update title session {session_id} nevratil vysledek.")
            conn.commit()
        return self._row_to_title_session(row)

    def update_effect_status(
        self,
        effect_id: str,
        *,
        effect_status: str,
        executed_at: str | None,
        effect_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Zmen stav jednoho effect queue radku a vrat aktualni payload."""

        with self._connect() as conn, conn.cursor() as cursor:
            serialized_payload = json.dumps(effect_payload, ensure_ascii=False, sort_keys=True) if effect_payload is not None else None
            cursor.execute(
                """
                UPDATE app.title_session_effect_queue
                SET
                    effect_status = %s,
                    executed_at = %s::timestamp,
                    effect_payload = CASE
                        WHEN %s::jsonb IS NULL THEN effect_payload
                        ELSE %s::jsonb
                    END
                WHERE effect_id = %s
                RETURNING
                    effect_id,
                    session_id,
                    action_id,
                    rule_id,
                    tconst,
                    effect_type,
                    phase,
                    source_list_id,
                    target_list_id,
                    effect_status,
                    effect_order,
                    effect_payload,
                    created_at,
                    executed_at
                """,
                (effect_status, executed_at, serialized_payload, serialized_payload, effect_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError(f"PostgreSQL update title session effect {effect_id} nevratil vysledek.")
            conn.commit()
        return self._row_to_title_session_effect(row)

    def _row_to_title_session(self, row: tuple[Any, ...]) -> dict[str, Any]:
        """Prevede PostgreSQL radek session na slovnik."""

        return {
            "session_id": row[0],
            "tconst": row[1],
            "status": row[2],
            "opened_from": row[3],
            "return_to_url": row[4],
            "source_list_id": row[5],
            "session_scope": row[6],
            "started_at": self._parse_optional_timestamp(row[7]),
            "updated_at": self._parse_optional_timestamp(row[8]),
            "finalized_at": self._parse_optional_timestamp(row[9]),
        }

    def _row_to_title_session_action(self, row: tuple[Any, ...]) -> dict[str, Any]:
        """Prevede PostgreSQL radek session akce na slovnik."""

        return {
            "action_id": row[0],
            "session_id": row[1],
            "tconst": row[2],
            "source_list_id": row[3],
            "trigger_action": row[4],
            "target_list_id": row[5],
            "rating_value": int(row[6]) if row[6] is not None else None,
            "notes_text": row[7],
            "action_payload": self._loads_json_or_default(row[8], default={}),
            "action_order": int(row[9]),
            "created_at": self._parse_optional_timestamp(row[10]),
        }

    def _row_to_title_session_effect(self, row: tuple[Any, ...]) -> dict[str, Any]:
        """Prevede PostgreSQL radek effect queue na slovnik."""

        return {
            "effect_id": row[0],
            "session_id": row[1],
            "action_id": row[2],
            "rule_id": row[3],
            "tconst": row[4],
            "effect_type": row[5],
            "phase": row[6],
            "source_list_id": row[7],
            "target_list_id": row[8],
            "effect_status": row[9],
            "effect_order": int(row[10]),
            "effect_payload": self._loads_json_or_default(row[11], default={}),
            "created_at": self._parse_optional_timestamp(row[12]),
            "executed_at": self._parse_optional_timestamp(row[13]),
        }

    def _row_to_list_action_rule(self, row: tuple[Any, ...]) -> dict[str, Any]:
        """Prevede PostgreSQL radek pravidla na slovnik."""

        return {
            "rule_id": row[0],
            "source_list_id": row[1],
            "trigger_action": row[2],
            "target_list_id": row[3],
            "effect_type": row[4],
            "phase": row[5],
            "order_index": int(row[6]),
            "enabled": bool(row[7]),
            "lock_reason_key": row[8],
            "lock_reason_text": row[9],
            "effect_params": self._loads_json_or_default(row[10], default={}),
            "created_at": self._parse_optional_timestamp(row[11]),
            "updated_at": self._parse_optional_timestamp(row[12]),
        }


class TitleSessionOrchestrator:
    """Sklada a vykonava prvni effect queue nad title session.

    Tohle je zamerne jen prvni rez. Vstupem je uz existujici session akce a
    odpovidajici pravidla. Orchestrator z nich slozi effect queue a umi
    zpracovat bezpecnou prvni sadu efektu nad existujicimi low-level helpery.
    """

    def __init__(
        self,
        *,
        store: TitleSessionStore,
        upsert_user_rating: Callable[..., Any],
        record_watched: Callable[..., Any],
        upsert_user_list_item: Callable[..., Any],
        archive_user_list_item: Callable[..., Any],
        archive_user_list_group: Callable[..., Any],
    ) -> None:
        """Uloz storage a efektove callbacky bez prime zavislosti na facade modulu."""

        self._store = store
        self._upsert_user_rating = upsert_user_rating
        self._record_watched = record_watched
        self._upsert_user_list_item = upsert_user_list_item
        self._archive_user_list_item = archive_user_list_item
        self._archive_user_list_group = archive_user_list_group

    def queue_action_effects(self, action_id: str, *, queued_at: str) -> dict[str, Any]:
        """Sestav a uloz effect queue pro jednu session akci."""

        action = self._store.fetch_action(action_id)
        if action is None:
            raise ValueError("Session akce nebyla nalezena.")
        rules = self._store.fetch_rules(
            source_list_id=str(action.get("source_list_id") or ""),
            trigger_action=str(action["trigger_action"]),
            target_list_id=action.get("target_list_id"),
            enabled_only=True,
        )
        if not rules:
            return {
                "session_id": action["session_id"],
                "action_id": action["action_id"],
                "queued_count": 0,
                "immediate_count": 0,
                "finalize_only_count": 0,
                "effects": [],
            }
        existing_queue = self._store.fetch_effect_queue(action["session_id"])
        next_order = (max((int(row["effect_order"]) for row in existing_queue), default=0) // 10 + 1) * 10
        queued_rows: list[dict[str, Any]] = []
        for rule in rules:
            effect_payload = self._build_effect_payload(action=action, rule=rule, queued_at=queued_at)
            queued_rows.append(
                {
                    "effect_id": f"title-session-effect:{uuid.uuid4()}",
                    "session_id": action["session_id"],
                    "action_id": action["action_id"],
                    "rule_id": rule["rule_id"],
                    "tconst": action["tconst"],
                    "effect_type": rule["effect_type"],
                    "phase": rule["phase"],
                    "source_list_id": action.get("source_list_id"),
                    "target_list_id": rule.get("target_list_id") or action.get("target_list_id"),
                    "effect_status": "pending",
                    "effect_order": next_order,
                    "effect_payload": effect_payload,
                    "created_at": queued_at,
                    "executed_at": None,
                }
            )
            next_order += 10
        self._store.insert_effect_rows(queued_rows)
        return {
            "session_id": action["session_id"],
            "action_id": action["action_id"],
            "queued_count": len(queued_rows),
            "immediate_count": sum(1 for row in queued_rows if row["phase"] == "immediate"),
            "finalize_only_count": sum(1 for row in queued_rows if row["phase"] == "finalize_only"),
            "effects": queued_rows,
        }

    def apply_effects(
        self,
        session_id: str,
        *,
        phase: str,
        executed_at: str,
        effect_status: str = "pending",
    ) -> dict[str, Any]:
        """Vykonej effect queue pro vybranou phase a vrat souhrn."""

        effects = self._store.fetch_effect_queue(session_id, phase=phase, effect_status=effect_status)
        applied = 0
        skipped = 0
        failed = 0
        results: list[dict[str, Any]] = []
        for effect in effects:
            try:
                outcome = self._apply_effect(effect, executed_at=executed_at)
            except Exception as exc:
                failed += 1
                updated = self._store.update_effect_status(
                    effect["effect_id"],
                    effect_status="failed",
                    executed_at=executed_at,
                    effect_payload={**dict(effect.get("effect_payload") or {}), "error": str(exc)},
                )
                results.append({"effect_id": effect["effect_id"], "result": "failed", "effect": updated})
                continue
            if outcome == "skipped":
                skipped += 1
            else:
                applied += 1
            updated = self._store.update_effect_status(
                effect["effect_id"],
                effect_status="skipped" if outcome == "skipped" else "applied",
                executed_at=executed_at,
                effect_payload=effect.get("effect_payload") or {},
            )
            results.append({"effect_id": effect["effect_id"], "result": outcome, "effect": updated})
        return {
            "session_id": session_id,
            "phase": phase,
            "applied_count": applied,
            "skipped_count": skipped,
            "failed_count": failed,
            "effects": results,
        }

    def finalize_session(self, session_id: str, *, finalized_at: str) -> dict[str, Any]:
        """Proved finalize_only effecty a session oznac jako finalizovanou."""

        session = self._store.update_session_status(
            session_id,
            status="finalizing",
            updated_at=finalized_at,
        )
        finalize_result = self.apply_effects(
            session_id,
            phase="finalize_only",
            executed_at=finalized_at,
            effect_status="pending",
        )
        session = self._store.update_session_status(
            session_id,
            status="finalized",
            updated_at=finalized_at,
            finalized_at=finalized_at,
        )
        return {
            "session": session,
            "finalize": finalize_result,
        }

    def _apply_effect(self, effect: dict[str, Any], *, executed_at: str) -> str:
        """Vykonej jeden effect radek pres existujici low-level helpery."""

        payload = dict(effect.get("effect_payload") or {})
        effect_type = str(effect["effect_type"])

        if effect_type in {"derive_watched", "preserve_source_membership", "preserve_target_membership", "noop"}:
            return "applied"

        if effect_type == "write_rating":
            rating_value = payload.get("rating_value")
            canonical_key = payload.get("canonical_key")
            media_type = payload.get("media_type")
            if rating_value is None or not canonical_key or not media_type:
                return "skipped"
            self._upsert_user_rating(
                canonical_key=str(canonical_key),
                tconst=payload.get("tconst"),
                media_type=str(media_type),
                imdb_id=payload.get("imdb_id"),
                tmdb_id=payload.get("tmdb_id"),
                trakt_id=payload.get("trakt_id"),
                parent_tconst=payload.get("parent_tconst"),
                parent_title=payload.get("parent_title"),
                title=payload.get("title"),
                season_number=payload.get("season_number"),
                episode_number=payload.get("episode_number"),
                rating=int(rating_value),
                rated_at=str(payload.get("rated_at") or executed_at),
                source_origin=str(payload.get("source_origin") or "title_session"),
                source_ref=payload.get("source_ref"),
                now=executed_at,
                liked_notes=payload.get("liked_notes"),
                disliked_notes=payload.get("disliked_notes"),
            )
            return "applied"

        if effect_type == "write_watched":
            event_scope = str(payload.get("event_scope") or "title")
            self._record_watched(
                event_id=str(payload.get("event_id") or f"title-session-watch:{uuid.uuid4()}"),
                tconst=str(payload.get("tconst") or effect["tconst"]),
                event_scope=event_scope,
                watched_on=str(payload.get("watched_on") or executed_at[:10]),
                notes=payload.get("notes"),
                created_at=executed_at,
                archive_from_list_id=None,
                archive_canonical_key=None,
                archive_display_tconst=None,
            )
            return "applied"

        if effect_type == "add_target_membership":
            target_list_id = effect.get("target_list_id")
            items = payload.get("group_items") or [payload]
            if not target_list_id:
                return "skipped"
            applied_any = False
            for item_payload in items:
                canonical_key = item_payload.get("canonical_key")
                media_type = item_payload.get("media_type")
                if not canonical_key or not media_type:
                    continue
                self._upsert_user_list_item(
                    item_id=str(item_payload.get("item_id") or f"title-session-item:{uuid.uuid4()}"),
                    list_id=str(target_list_id),
                    canonical_key=str(canonical_key),
                    tconst=item_payload.get("tconst"),
                    media_type=str(media_type),
                    imdb_id=item_payload.get("imdb_id"),
                    tmdb_id=item_payload.get("tmdb_id"),
                    trakt_id=item_payload.get("trakt_id"),
                    parent_tconst=item_payload.get("parent_tconst"),
                    parent_title=item_payload.get("parent_title"),
                    title=item_payload.get("title"),
                    season_number=item_payload.get("season_number"),
                    episode_number=item_payload.get("episode_number"),
                    rank=item_payload.get("rank"),
                    added_at=item_payload.get("added_at") or executed_at,
                    notes=item_payload.get("notes"),
                    source_origin=str(item_payload.get("source_origin") or payload.get("source_origin") or "title_session"),
                    source_ref=item_payload.get("source_ref") or payload.get("source_ref"),
                    now=executed_at,
                )
                applied_any = True
            return "applied" if applied_any else "skipped"

        if effect_type == "deactivate_source_membership":
            source_list_id = effect.get("source_list_id")
            canonical_key = payload.get("canonical_key")
            display_tconst = payload.get("display_tconst")
            if source_list_id and canonical_key:
                self._archive_user_list_item(str(source_list_id), str(canonical_key), executed_at)
                return "applied"
            if source_list_id and display_tconst:
                self._archive_user_list_group(
                    list_id=str(source_list_id),
                    display_tconst=str(display_tconst),
                    now=executed_at,
                )
                return "applied"
            return "skipped"

        if effect_type in {"remove_source_membership", "remove_target_membership"}:
            return "skipped"

        return "skipped"

    @staticmethod
    def _build_effect_payload(*, action: dict[str, Any], rule: dict[str, Any], queued_at: str) -> dict[str, Any]:
        """Sloz payload pro effect queue z akce a pravidla."""

        payload = dict(action.get("action_payload") or {})
        payload.setdefault("tconst", action["tconst"])
        payload.setdefault("source_list_id", action.get("source_list_id"))
        payload.setdefault("target_list_id", rule.get("target_list_id") or action.get("target_list_id"))
        payload.setdefault("trigger_action", action["trigger_action"])
        payload.setdefault("queued_at", queued_at)
        if action.get("rating_value") is not None:
            payload.setdefault("rating_value", action["rating_value"])
        if action.get("notes_text") is not None:
            payload.setdefault("notes_text", action["notes_text"])
        if rule.get("effect_type") == "derive_watched":
            payload.setdefault("derived_interest_state", "watched")
        return payload
