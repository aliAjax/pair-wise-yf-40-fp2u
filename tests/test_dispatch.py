import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _service(db_path):
    repo = SQLiteRepository(db_path)
    return DomainService(repo, RuleEngine())


def _declared_quarantined(service, actor, code, slot_code="A-01", creator=None):
    slot = service.register_slot(actor, {"code": slot_code, "zone": "棚1"})
    consignment = service.create(
        creator or actor, "consignment",
        {"code": code, "origin": "O", "destination": "D"},
    )
    service.transition(
        creator or actor, consignment["id"], "inspect",
        {"inspector": "I-1", "inspection_result": "suspected"},
    )
    consignment = service.transition(
        actor, consignment["id"], "quarantine",
        {
            "pest_found": True,
            "sample_id": "S-1",
            "slot_id": slot["id"],
            "disposal_method": "incineration",
        },
    )
    return slot, consignment


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.service = _service(self.db)
        self.actor = Actor("q-ops", "quarantine")
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_slot_is_persisted_and_listed(self):
        slot = self.service.register_slot(self.actor, {"code": "B-02", "zone": "棚2"})
        self.assertEqual(slot["status"], "available")
        codes = [item["code"] for item in self.service.list_slots()]
        self.assertIn("B-02", codes)

    def test_duplicate_slot_code_rejected(self):
        self.service.register_slot(self.actor, {"code": "A-01"})
        with self.assertRaises(ConflictError):
            self.service.register_slot(self.actor, {"code": "A-01"})

    def test_quarantine_occupies_slot_and_records_method(self):
        slot, consignment = _declared_quarantined(self.service, self.actor, "C-1", creator=self.admin)
        occupied = self.service.get_slot(slot["id"])
        self.assertEqual(occupied["status"], "occupied")
        self.assertEqual(occupied["consignment_id"], consignment["id"])
        self.assertEqual(occupied["consignment_code"], "C-1")
        self.assertEqual(occupied["disposal_method"], "incineration")
        self.assertEqual(occupied["occupied_by"], "q-ops")
        self.assertEqual(consignment["data"]["slot_id"], slot["id"])
        self.assertEqual(consignment["data"]["slot_code"], "A-01")

    def test_occupied_slot_cannot_take_second_consignment(self):
        _, first = _declared_quarantined(self.service, self.actor, "C-1", creator=self.admin)
        second = self.service.create(
            self.admin, "consignment",
            {"code": "C-2", "origin": "O2", "destination": "D2"},
        )
        self.service.transition(
            self.admin, second["id"], "inspect",
            {"inspector": "I-1", "inspection_result": "positive"},
        )
        slot = self.service.list_slots()[0]
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.actor, second["id"], "quarantine",
                {
                    "pest_found": True,
                    "sample_id": "S-2",
                    "slot_id": slot["id"],
                    "disposal_method": "deep_burial",
                },
            )
        # 安排失败不应改变批次状态，也不应留下占格记录
        self.assertEqual(
            self.service.get(second["id"])["status"], "inspected"
        )
        self.assertEqual(self.service.get_slot(slot["id"])["consignment_id"], first["id"])

    def test_repeated_quarantine_submission_reuses_order(self):
        slot, consignment = _declared_quarantined(self.service, self.actor, "C-1", creator=self.admin)
        version_before = consignment["version"]
        repeated = self.service.transition(
            self.actor, consignment["id"], "quarantine",
            {
                "pest_found": True,
                "sample_id": "S-1",
                "slot_id": slot["id"],
                "disposal_method": "incineration",
            },
            expected_version=consignment["version"],
        )
        self.assertEqual(repeated["id"], consignment["id"])
        self.assertEqual(repeated["version"], version_before)
        events = self.service.slot_history(slot["id"])
        self.assertEqual([e["action"] for e in events].count("assign"), 1)

    def test_destroy_releases_slot_with_reason_and_operator(self):
        slot, consignment = _declared_quarantined(self.service, self.actor, "C-1", creator=self.admin)
        self.service.transition(
            self.actor, consignment["id"], "destroy",
            {"witnessed_by": "W-1", "reason": "焚烧完毕"},
        )
        freed = self.service.get_slot(slot["id"])
        self.assertEqual(freed["status"], "available")
        self.assertIsNone(freed["consignment_id"])
        history = self.service.slot_history(slot["id"])
        release = history[0]
        self.assertEqual(release["action"], "release")
        self.assertIn("处置完成", release["reason"])
        self.assertIn("焚烧完毕", release["reason"])
        self.assertEqual(release["actor_id"], "q-ops")
        self.assertEqual(release["disposal_method"], "焚烧")

    def test_recheck_releases_slot_and_returns_to_inspected(self):
        slot, consignment = _declared_quarantined(self.service, self.actor, "C-1", creator=self.admin)
        updated = self.service.transition(
            Actor("ins-2", "inspector"), consignment["id"], "recheck",
            {"sample_id": "S-9", "reason": "复检阴性，转回普通检疫"},
        )
        self.assertEqual(updated["status"], "inspected")
        self.assertEqual(self.service.get_slot(slot["id"])["status"], "available")
        release = self.service.slot_history(slot["id"])[0]
        self.assertIn("复检转回普通检疫", release["reason"])
        self.assertIn("复检阴性", release["reason"])
        self.assertEqual(release["actor_id"], "ins-2")
        # 释放后同一格可安排新批次
        slot2 = self.service.list_slots()[0]
        second = self.service.create(
            self.admin, "consignment",
            {"code": "C-3", "origin": "O3", "destination": "D3"},
        )
        self.service.transition(
            self.admin, second["id"], "inspect",
            {"inspector": "I-1", "inspection_result": "positive"},
        )
        again = self.service.transition(
            self.actor, second["id"], "quarantine",
            {
                "pest_found": True,
                "sample_id": "S-3",
                "slot_id": slot2["id"],
                "disposal_method": "deep_burial",
            },
        )
        self.assertEqual(again["status"], "quarantined")
        self.assertEqual(
            self.service.get_slot(slot2["id"])["consignment_code"], "C-3"
        )

    def test_quarantine_requires_valid_method(self):
        slot = self.service.register_slot(self.actor, {"code": "A-01"})
        consignment = self.service.create(
            self.admin, "consignment",
            {"code": "C-9", "origin": "O", "destination": "D"},
        )
        self.service.transition(
            self.admin, consignment["id"], "inspect",
            {"inspector": "I-1", "inspection_result": "suspected"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.actor, consignment["id"], "quarantine",
                {
                    "pest_found": True,
                    "sample_id": "S-1",
                    "slot_id": slot["id"],
                    "disposal_method": "unknown",
                },
            )

    def test_state_survives_restart(self):
        slot, consignment = _declared_quarantined(
            self.service, self.actor, "C-PERSIST", creator=self.admin
        )
        slot_id, consignment_id = slot["id"], consignment["id"]

        restarted = _service(self.db)
        slot_after = restarted.get_slot(slot_id)
        self.assertEqual(slot_after["status"], "occupied")
        self.assertEqual(slot_after["consignment_id"], consignment_id)
        self.assertEqual(
            restarted.get(consignment_id)["data"]["slot_code"], "A-01"
        )
        self.assertTrue(restarted.slot_history())


class SlotHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        service = _service(self.db)
        static_dir = str(Path(__file__).resolve().parent.parent / "static")
        self.server = create_server("127.0.0.1", 0, service, RuleEngine(), static_dir)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def _request(self, method, path, body=None, role="quarantine", user="q-web"):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            "http://127.0.0.1:%s%s" % (self.port, path),
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "X-User-Id": user,
                "X-Role": role,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_slot_dispatch_over_http(self):
        status, slot = self._request(
            "POST", "/api/slots", {"code": "W-01", "zone": "web棚"}
        )
        self.assertEqual(status, 201)

        status, consignment = self._request(
            "POST", "/api/consignments",
            {"code": "C-W1", "origin": "O", "destination": "D"},
            role="inspector", user="ins-web",
        )
        self.assertEqual(status, 201)
        cid = consignment["id"]
        self._request(
            "POST", "/api/entities/%s/actions" % cid,
            {"action": "inspect",
             "data": {"inspector": "ins-web", "inspection_result": "positive"}},
            role="inspector", user="ins-web",
        )
        status, quarantined = self._request(
            "POST", "/api/entities/%s/actions" % cid,
            {"action": "quarantine", "data": {
                "pest_found": True, "sample_id": "S-W1",
                "slot_id": slot["id"], "disposal_method": "deep_burial",
            }},
        )
        self.assertEqual(status, 200)
        self.assertEqual(quarantined["data"]["slot_code"], "W-01")

        # 重复提交沿用原单
        status, repeated = self._request(
            "POST", "/api/entities/%s/actions" % cid,
            {"action": "quarantine", "data": {
                "pest_found": True, "sample_id": "S-W1",
                "slot_id": slot["id"], "disposal_method": "deep_burial",
            }, "expected_version": quarantined["version"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(repeated["version"], quarantined["version"])

        # 第二批次排到同一格被拒绝
        status, second = self._request(
            "POST", "/api/consignments",
            {"code": "C-W2", "origin": "O2", "destination": "D2"},
            role="inspector", user="ins-web",
        )
        cid2 = second["id"]
        self._request(
            "POST", "/api/entities/%s/actions" % cid2,
            {"action": "inspect",
             "data": {"inspector": "ins-web", "inspection_result": "positive"}},
            role="inspector", user="ins-web",
        )
        status, failure = self._request(
            "POST", "/api/entities/%s/actions" % cid2,
            {"action": "quarantine", "data": {
                "pest_found": True, "sample_id": "S-W2",
                "slot_id": slot["id"], "disposal_method": "incineration",
            }},
        )
        self.assertEqual(status, 409)
        self.assertEqual(failure["type"], "ConflictError")

        # 销毁释放
        status, _ = self._request(
            "POST", "/api/entities/%s/actions" % cid,
            {"action": "destroy",
             "data": {"witnessed_by": "W-W1", "reason": "深埋完成"}},
        )
        self.assertEqual(status, 200)
        status, slots = self._request("GET", "/api/slots")
        self.assertEqual(slots["items"][0]["status"], "available")
        status, history = self._request(
            "GET", "/api/slots/%s/history" % slot["id"]
        )
        self.assertEqual(status, 200)
        reasons = [item["reason"] for item in history["items"]]
        self.assertTrue(any("深埋完成" in item for item in reasons))


if __name__ == "__main__":
    unittest.main()
