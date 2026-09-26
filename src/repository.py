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
                CREATE TABLE IF NOT EXISTS locations (
                    code TEXT PRIMARY KEY,
                    name TEXT NOT NULL DEFAULT '',
                    zone TEXT NOT NULL DEFAULT '',
                    registered_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS location_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    location_code TEXT NOT NULL,
                    consignment_id TEXT NOT NULL,
                    consignment_code TEXT NOT NULL DEFAULT '',
                    event TEXT NOT NULL,
                    disposal_method TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_location_events_code
                    ON location_events(location_code, id);
                CREATE INDEX IF NOT EXISTS idx_location_events_consignment
                    ON location_events(consignment_id, id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_location_open_code
                    ON location_events(location_code) WHERE event = 'occupied';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_location_open_consignment
                    ON location_events(consignment_id) WHERE event = 'occupied';
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

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

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

    def update_entity(self, entity_id, expected_version, status, data):
        with self.transaction() as connection:
            self._update_entity_on(connection, entity_id, expected_version, status, data)
        return self.get_entity(entity_id)

    @staticmethod
    def _update_entity_on(connection, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
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

    # ----- 库位（隔离棚格位）-----

    @staticmethod
    def _location_from_row(row):
        return {
            "code": row["code"],
            "name": row["name"],
            "zone": row["zone"],
            "registered_by": row["registered_by"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _location_event_from_row(row):
        return {
            "id": row["id"],
            "location_code": row["location_code"],
            "consignment_id": row["consignment_id"],
            "consignment_code": row["consignment_code"],
            "event": row["event"],
            "disposal_method": row["disposal_method"],
            "reason": row["reason"],
            "actor_id": row["actor_id"],
            "actor_role": row["actor_role"],
            "created_at": row["created_at"],
        }

    def register_location(self, code, name, zone, actor_id):
        now = utcnow()
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO locations(code, name, zone, registered_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (code, name, zone, actor_id, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("location already registered: " + code) from exc
        return self.get_location(code)

    def get_location(self, code):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM locations WHERE code = ?", (code,)
            ).fetchone()
        return self._location_from_row(row) if row else None

    def list_locations(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM locations ORDER BY code"
            ).fetchall()
            open_rows = connection.execute(
                "SELECT * FROM location_events WHERE event = 'occupied'"
            ).fetchall()
        occupancy = {
            row["location_code"]: self._location_event_from_row(row)
            for row in open_rows
        }
        locations = []
        for row in rows:
            location = self._location_from_row(row)
            event = occupancy.get(row["code"])
            location["status"] = "occupied" if event else "free"
            location["occupancy"] = event
            locations.append(location)
        return locations

    def get_open_occupancy(self, consignment_id=None, location_code=None):
        clauses = ["event = 'occupied'"]
        params = []
        if consignment_id is not None:
            clauses.append("consignment_id = ?")
            params.append(consignment_id)
        if location_code is not None:
            clauses.append("location_code = ?")
            params.append(location_code)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM location_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY id",
                params,
            ).fetchone()
        return self._location_event_from_row(row) if row else None

    def occupy_location(
        self, connection, location_code, consignment_id, consignment_code,
        disposal_method, actor,
    ):
        if connection.execute(
            "SELECT 1 FROM locations WHERE code = ?", (location_code,)
        ).fetchone() is None:
            raise NotFoundError("location not registered: " + location_code)
        existing_batch = connection.execute(
            "SELECT location_code FROM location_events "
            "WHERE event = 'occupied' AND consignment_id = ?",
            (consignment_id,),
        ).fetchone()
        if existing_batch:
            return False, self._location_event_from_row(existing_batch)
        now = utcnow()
        try:
            cursor = connection.execute(
                "INSERT INTO location_events(location_code, consignment_id, consignment_code, "
                "event, disposal_method, reason, actor_id, actor_role, created_at) "
                "VALUES (?, ?, ?, 'occupied', ?, '', ?, ?, ?)",
                (
                    location_code, consignment_id, consignment_code,
                    disposal_method, actor.user_id, actor.role, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "location already occupied: " + location_code
            ) from exc
        row = connection.execute(
            "SELECT * FROM location_events WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return True, self._location_event_from_row(row)

    def release_location(self, connection, consignment_id, reason, actor):
        row = connection.execute(
            "SELECT * FROM location_events WHERE event = 'occupied' AND consignment_id = ?",
            (consignment_id,),
        ).fetchone()
        if row is None:
            return None
        occupied = self._location_event_from_row(row)
        now = utcnow()
        connection.execute(
            "UPDATE location_events SET event = 'released', reason = ?, actor_id = ?, "
            "actor_role = ?, created_at = ? WHERE id = ?",
            (reason, actor.user_id, actor.role, now, row["id"]),
        )
        return occupied

    def list_location_events(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM location_events ORDER BY id"
            ).fetchall()
        return [self._location_event_from_row(row) for row in rows]

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
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
