from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import RuleEngine
from .scheduler import LocationScheduler


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)
        self.scheduler = LocationScheduler(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        payload = dict(data or {})

        if entity["kind"] == "consignment" and action == "quarantine":
            return self._quarantine(actor, entity, expected, payload)
        if entity["kind"] == "consignment" and action in ("destroy", "recheck"):
            return self._finish_disposal(actor, entity, expected, action, payload)

        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _quarantine(self, actor, entity, expected, payload):
        """批次转处置：选格 + 处置方式。重复提交沿用原单，不重复占格。"""
        existing = self.scheduler.open_occupancy(entity["id"])
        if existing is not None:
            return self.repository.get_entity(entity["id"])
        next_status, patch = self.rules.validate_transition(
            actor, entity, "quarantine", payload, self._lookup
        )
        location_code = patch["location_code"]
        disposal_method = patch["disposal_method"]
        if self.repository.get_location(location_code) is None:
            raise ValidationError("location not registered: " + location_code)
        merged = dict(entity["data"])
        merged.update(patch)
        with self.repository.transaction() as connection:
            created, occupancy = self.scheduler.occupy(
                connection, entity, location_code, disposal_method, actor
            )
            if not created:
                connection.rollback()
                return self.repository.get_entity(entity["id"])
            self.repository._update_entity_on(
                connection, entity["id"], expected, next_status, merged
            )
        updated = self.repository.get_entity(entity["id"])
        self.audit.record(
            entity["id"], actor, "quarantine", entity["status"],
            updated["status"],
            {"patch": patch, "location_code": occupancy["location_code"]},
        )
        return updated

    def _finish_disposal(self, actor, entity, expected, action, payload):
        """处置完成（destroy）或复检转回（recheck）：状态机推进并释放库位。"""
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
        )
        occupancy = self.scheduler.open_occupancy(entity["id"])
        merged = dict(entity["data"])
        merged.update(patch)
        if occupancy is not None:
            reason = str(payload.get("release_reason") or "").strip() or \
                self.scheduler.default_reason(action)
            merged["release_reason"] = reason
            with self.repository.transaction() as connection:
                released = self.repository.release_location(
                    connection, entity["id"], reason, actor
                )
                self.repository._update_entity_on(
                    connection, entity["id"], expected, next_status, merged
                )
        else:
            released = None
            self.repository.update_entity(
                entity["id"], expected, next_status, merged
            )
        updated = self.repository.get_entity(entity["id"])
        detail = {"patch": patch}
        if released is not None:
            detail["released_location"] = released["location_code"]
            detail["release_reason"] = merged["release_reason"]
        self.audit.record(
            entity["id"], actor, action, entity["status"],
            updated["status"], detail,
        )
        return updated

    # ----- 库位登记与调度台查询 -----

    def register_location(self, actor, data):
        payload = self.rules.validate_location_registration(actor, data)
        return self.scheduler.register_location(actor, payload)

    def list_locations(self):
        return self.scheduler.list_locations()

    def location_events(self):
        return self.scheduler.list_events()

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
