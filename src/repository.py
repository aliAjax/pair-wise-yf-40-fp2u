import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS slots (
                    id TEXT PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    zone TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'available'
                        CHECK(status IN ('available', 'occupied')),
                    consignment_id TEXT,
                    consignment_code TEXT,
                    disposal_method TEXT,
                    occupied_by TEXT,
                    occupied_at TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS slot_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_id TEXT NOT NULL,
                    slot_code TEXT NOT NULL,
                    consignment_id TEXT,
                    consignment_code TEXT,
                    action TEXT NOT NULL
                        CHECK(action IN ('assign', 'release')),
                    disposal_method TEXT,
                    reason TEXT,
                    actor_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_slot_history_slot
                    ON slot_history(slot_id, id);
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _slot_from_row(row):
        return {
            "id": row["id"],
            "code": row["code"],
            "zone": row["zone"],
            "status": row["status"],
            "consignment_id": row["consignment_id"],
            "consignment_code": row["consignment_code"],
            "disposal_method": row["disposal_method"],
            "occupied_by": row["occupied_by"],
            "occupied_at": row["occupied_at"],
            "version": int(row["version"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ---- transactions -----------------------------------------------------

    @contextmanager
    def transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # ---- generic entities -------------------------------------------------

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def _get_entity(self, connection, entity_id):
        row = connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return self._entity_from_row(row) if row else None

    def get_entity(self, entity_id, conn=None):
        if conn is not None:
            return self._get_entity(conn, entity_id)
        with self._connect() as connection:
            return self._get_entity(connection, entity_id)

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def _update_entity(self, connection, entity_id, expected_version, status, payload):
        now = utcnow()
        row = connection.execute(
            "SELECT version FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("entity not found: " + entity_id)
        current_version = int(row["version"])
        if expected_version is not None and current_version != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, current_version)
            )
        connection.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
            "WHERE id = ? AND version = ?",
            (status, payload, now, entity_id, current_version),
        )
        return self._get_entity(connection, entity_id)

    def update_entity(self, entity_id, expected_version, status, data, conn=None):
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if conn is not None:
            return self._update_entity(conn, entity_id, expected_version, status, payload)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = self._update_entity(
                connection, entity_id, expected_version, status, payload
            )
            connection.commit()
            return result
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    # ---- quarantine slots (库位记录) --------------------------------------

    def insert_slot(self, slot_id, code, zone, actor_id, conn=None):
        now = utcnow()
        params = (slot_id, code, zone, actor_id, now, now)
        sql = (
            "INSERT INTO slots(id, code, zone, status, version, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 'available', 1, ?, ?, ?)"
        )
        try:
            if conn is not None:
                conn.execute(sql, params)
                return self._get_slot(conn, slot_id)
            with self._connect() as connection:
                connection.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("slot code already registered: " + code) from exc
        return self.get_slot(slot_id)

    def _get_slot(self, connection, slot_id):
        row = connection.execute(
            "SELECT * FROM slots WHERE id = ?", (slot_id,)
        ).fetchone()
        return self._slot_from_row(row) if row else None

    def get_slot(self, slot_id, conn=None):
        if conn is not None:
            return self._get_slot(conn, slot_id)
        with self._connect() as connection:
            return self._get_slot(connection, slot_id)

    def get_slot_by_code(self, code, conn=None):
        sql = "SELECT * FROM slots WHERE code = ?"
        if conn is not None:
            row = conn.execute(sql, (code,)).fetchone()
            return self._slot_from_row(row) if row else None
        with self._connect() as connection:
            row = connection.execute(sql, (code,)).fetchone()
        return self._slot_from_row(row) if row else None

    def list_slots(self, conn=None):
        sql = "SELECT * FROM slots ORDER BY created_at, code"
        if conn is not None:
            rows = conn.execute(sql).fetchall()
            return [self._slot_from_row(row) for row in rows]
        with self._connect() as connection:
            rows = connection.execute(sql).fetchall()
        return [self._slot_from_row(row) for row in rows]

    def occupy_slot(self, conn, slot_id, consignment_id, consignment_code,
                    disposal_method, actor_id):
        """条件占用：仅当库位仍空闲时生效，返回受影响行数。"""
        now = utcnow()
        cursor = conn.execute(
            "UPDATE slots SET status = 'occupied', consignment_id = ?, "
            "consignment_code = ?, disposal_method = ?, occupied_by = ?, "
            "occupied_at = ?, version = version + 1, updated_at = ? "
            "WHERE id = ? AND status = 'available'",
            (
                consignment_id,
                consignment_code,
                disposal_method,
                actor_id,
                now,
                now,
                slot_id,
            ),
        )
        return cursor.rowcount

    def free_slot(self, conn, slot_id, consignment_id, actor_id):
        """条件释放：仅当库位由该批次占用时生效，返回受影响行数。"""
        now = utcnow()
        cursor = conn.execute(
            "UPDATE slots SET status = 'available', consignment_id = NULL, "
            "consignment_code = NULL, disposal_method = NULL, occupied_by = NULL, "
            "occupied_at = NULL, version = version + 1, updated_at = ? "
            "WHERE id = ? AND status = 'occupied' AND consignment_id = ?",
            (now, slot_id, consignment_id),
        )
        return cursor.rowcount

    def add_slot_event(self, conn, slot_id, slot_code, action, consignment_id,
                       consignment_code, disposal_method, reason, actor_id):
        conn.execute(
            "INSERT INTO slot_history(slot_id, slot_code, consignment_id, "
            "consignment_code, action, disposal_method, reason, actor_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                slot_id,
                slot_code,
                consignment_id,
                consignment_code,
                action,
                disposal_method,
                reason,
                actor_id,
                utcnow(),
            ),
        )

    def list_slot_history(self, slot_id=None, conn=None):
        clauses = []
        params = []
        if slot_id:
            clauses.append("slot_id = ?")
            params.append(slot_id)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT * FROM slot_history" + where + " ORDER BY id DESC"
        if conn is not None:
            rows = conn.execute(sql, params).fetchall()
        else:
            with self._connect() as connection:
                rows = connection.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "slot_id": row["slot_id"],
                "slot_code": row["slot_code"],
                "consignment_id": row["consignment_id"],
                "consignment_code": row["consignment_code"],
                "action": row["action"],
                "disposal_method": row["disposal_method"],
                "reason": row["reason"],
                "actor_id": row["actor_id"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ---- audit ------------------------------------------------------------

    def _append_audit(self, connection, entity_id, actor_id, actor_role, action,
                      from_status, to_status, detail):
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                utcnow(),
            ),
        )

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status,
                     to_status, detail, conn=None):
        if conn is not None:
            return self._append_audit(
                conn, entity_id, actor_id, actor_role, action,
                from_status, to_status, detail,
            )
        with self._connect() as connection:
            self._append_audit(
                connection, entity_id, actor_id, actor_role, action,
                from_status, to_status, detail,
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ---- idempotency ------------------------------------------------------

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
