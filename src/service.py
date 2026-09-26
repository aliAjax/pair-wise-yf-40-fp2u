from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError
from .rules import DISPOSAL_METHOD_LABELS, RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        kind = self.rules.normalize_kind(kind)
        if kind == "slot":
            if field == "id":
                row = self.repository.get_slot(value)
                return [row] if row else []
            if field == "code":
                rows = self.repository.list_slots()
                return [row for row in rows if row.get(field) == value]
            return []
        return self.repository.find_entities(kind, field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ---- generic entity creation -----------------------------------------

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind == "slot":
            return self.register_slot(actor, data or {}, idempotency_key)
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

    # ---- quarantine slots (库位登记) --------------------------------------

    def register_slot(self, actor, data, idempotency_key=None):
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                slot = self.repository.get_slot(existing)
                if slot:
                    return slot
        self.rules.validate_create(actor, "slot", payload, self._lookup)
        slot_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_slot(slot_id):
            raise ConflictError("slot already exists: " + slot_id)
        zone = payload.get("zone", "") or ""
        slot = self.repository.insert_slot(
            slot_id, payload["code"], zone, actor.user_id
        )
        self.audit.record(
            slot_id, actor, "register_slot", None, "available",
            {"code": slot["code"], "zone": zone},
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, slot_id)
        return slot

    def list_slots(self):
        return self.repository.list_slots()

    def get_slot(self, slot_id):
        slot = self.repository.get_slot(slot_id)
        if not slot:
            raise NotFoundError("slot not found: " + slot_id)
        return slot

    def slot_history(self, slot_id=None):
        return self.repository.list_slot_history(slot_id=slot_id)

    # ---- transitions ------------------------------------------------------

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "consignment" and action in (
            "quarantine", "destroy", "recheck"
        ):
            return self._consignment_disposal(
                actor, entity, action, dict(data or {}), expected_version
            )

        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
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

    def _consignment_disposal(self, actor, entity, action, data, expected_version):
        """处置调度：批次状态与库位占用在同一事务内联动。"""
        expected = (
            int(expected_version) if expected_version is not None else entity["version"]
        )
        with self.repository.transaction() as conn:
            # 事务内重读，避免并发安排到同一格
            locked = self.repository.get_entity(entity["id"], conn=conn)
            if locked["version"] != expected:
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected, locked["version"])
                )

            # 同一批次重复提交沿用原单，不重复占格
            if action == "quarantine" and locked["status"] == "quarantined":
                slot_id = locked["data"].get("slot_id")
                slot = self.repository.get_slot(slot_id, conn=conn) if slot_id else None
                if slot and slot.get("consignment_id") == locked["id"]:
                    return locked
                raise ConflictError(
                    "consignment already quarantined but slot %s is no longer assigned"
                    % slot_id
                )

            next_status, patch = self.rules.validate_transition(
                actor, locked, action, data, self._lookup
            )

            if action == "quarantine":
                slot = self.repository.get_slot(patch["slot_id"], conn=conn)
                # 条件 UPDATE 兜底：同格未清空前不再安排
                changed = self.repository.occupy_slot(
                    conn,
                    slot["id"],
                    locked["id"],
                    locked["data"].get("code"),
                    patch["disposal_method"],
                    actor.user_id,
                )
                if not changed:
                    raise ConflictError(
                        "slot %s is occupied and has not been cleared" % slot["code"]
                    )
                self.repository.add_slot_event(
                    conn,
                    slot["id"],
                    slot["code"],
                    "assign",
                    locked["id"],
                    locked["data"].get("code"),
                    DISPOSAL_METHOD_LABELS.get(
                        patch["disposal_method"], patch["disposal_method"]
                    ),
                    "转处置",
                    actor.user_id,
                )
            else:
                slot_id = locked["data"]["slot_id"]
                slot = self.repository.get_slot(slot_id, conn=conn)
                if not slot:
                    raise NotFoundError("slot not found: " + str(slot_id))
                changed = self.repository.free_slot(
                    conn, slot["id"], locked["id"], actor.user_id
                )
                if not changed:
                    raise ConflictError(
                        "slot %s is not currently occupied by this consignment"
                        % slot["code"]
                    )
                label = self.rules.SLOT_RELEASE_ACTIONS[action]
                method = patch.get("method") or locked["data"].get("disposal_method")
                if action == "recheck":
                    reason = "%s：%s" % (label, patch.get("release_reason", ""))
                else:
                    reason = label
                    if patch.get("release_reason"):
                        reason += "：" + patch["release_reason"]
                self.repository.add_slot_event(
                    conn,
                    slot["id"],
                    slot["code"],
                    "release",
                    locked["id"],
                    locked["data"].get("code"),
                    DISPOSAL_METHOD_LABELS.get(method, method),
                    reason,
                    actor.user_id,
                )

            merged = dict(locked["data"])
            merged.update(patch)
            updated = self.repository.update_entity(
                locked["id"], expected, next_status, merged, conn=conn
            )
            self.repository.append_audit(
                locked["id"],
                actor.user_id,
                actor.role,
                action,
                locked["status"],
                updated["status"],
                {"patch": patch, "slot_code": slot["code"]},
                conn=conn,
            )
        return updated

    # ---- reads ------------------------------------------------------------

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
