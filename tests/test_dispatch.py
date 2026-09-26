import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.quar = Actor("quar-1", "quarantine")
        self.insp = Actor("inspector-1", "inspector")

    def tearDown(self):
        self.tmp.cleanup()

    def _make_inspected(self, code):
        entity = self.service.create(
            self.admin,
            "consignment",
            {"code": code, "origin": "Port-A", "destination": "Farm-B"},
        )
        return self.service.transition(
            self.insp,
            entity["id"],
            "inspect",
            {"inspector": "inspector-1", "inspection_result": "suspected"},
        )

    def test_register_location_and_quarantine_occupies_cell(self):
        self.service.register_location(
            self.quar, {"code": "A-01", "name": "一号格", "zone": "甲区"}
        )
        batch = self._make_inspected("C-100")
        self.service.transition(
            self.quar,
            batch["id"],
            "quarantine",
            {
                "pest_found": True,
                "sample_id": "S-100",
                "location_code": "A-01",
                "disposal_method": "incineration",
            },
        )
        locations = {item["code"]: item for item in self.service.list_locations()}
        self.assertEqual(locations["A-01"]["status"], "occupied")
        self.assertEqual(
            locations["A-01"]["occupancy"]["consignment_code"], "C-100"
        )

    def test_same_cell_rejects_second_batch_until_cleared(self):
        self.service.register_location(self.quar, {"code": "A-01"})
        first = self._make_inspected("C-1")
        second = self._make_inspected("C-2")
        quarantine_data = {
            "pest_found": True,
            "sample_id": "S-x",
            "location_code": "A-01",
            "disposal_method": "deep_burial",
        }
        self.service.transition(self.quar, first["id"], "quarantine", dict(quarantine_data, sample_id="S-1"))
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.quar, second["id"], "quarantine", dict(quarantine_data, sample_id="S-2")
            )
        # 销毁完成后格子腾空，第二批才能排进去
        self.service.transition(
            self.quar,
            first["id"],
            "destroy",
            {"method": "incineration", "witnessed_by": "W-1"},
        )
        self.assertEqual(
            {item["code"]: item["status"] for item in self.service.list_locations()}["A-01"],
            "free",
        )
        self.service.transition(
            self.quar, second["id"], "quarantine", dict(quarantine_data, sample_id="S-2")
        )
        self.assertEqual(
            self.service.list_locations()[0]["occupancy"]["consignment_code"], "C-2"
        )

    def test_duplicate_quarantine_submission_reuses_order(self):
        self.service.register_location(self.quar, {"code": "A-01"})
        self.service.register_location(self.quar, {"code": "A-02"})
        batch = self._make_inspected("C-7")
        data = {
            "pest_found": True,
            "sample_id": "S-7",
            "location_code": "A-01",
            "disposal_method": "chemical",
        }
        first = self.service.transition(self.quar, batch["id"], "quarantine", dict(data))
        # 重复提交（甚至想换格）沿用原单：状态不变、仍占 A-01，A-02 仍空
        second = self.service.transition(
            self.quar, batch["id"], "quarantine", dict(data, location_code="A-02")
        )
        self.assertEqual(first["version"], second["version"])
        self.assertEqual(second["status"], "quarantined")
        statuses = {item["code"]: item["status"] for item in self.service.list_locations()}
        self.assertEqual(statuses["A-01"], "occupied")
        self.assertEqual(statuses["A-02"], "free")

    def test_recheck_releases_cell_with_reason_and_actor(self):
        self.service.register_location(self.quar, {"code": "B-03"})
        batch = self._make_inspected("C-9")
        self.service.transition(
            self.quar,
            batch["id"],
            "quarantine",
            {
                "pest_found": True,
                "sample_id": "S-9",
                "location_code": "B-03",
                "disposal_method": "sterilization",
            },
        )
        updated = self.service.transition(
            self.insp,
            batch["id"],
            "recheck",
            {"sample_id": "S-9-R", "release_reason": "复检阴性，转回普通检疫"},
        )
        self.assertEqual(updated["status"], "inspected")
        self.assertEqual(
            self.service.list_locations()[0]["status"], "free"
        )
        events = self.service.location_events()
        release = [event for event in events if event["event"] == "released"][0]
        self.assertEqual(release["reason"], "复检阴性，转回普通检疫")
        self.assertEqual(release["actor_id"], "inspector-1")
        self.assertEqual(release["location_code"], "B-03")

    def test_destroy_records_default_reason_and_operator(self):
        self.service.register_location(self.quar, {"code": "C-02"})
        batch = self._make_inspected("C-10")
        self.service.transition(
            self.quar,
            batch["id"],
            "quarantine",
            {
                "pest_found": True,
                "sample_id": "S-10",
                "location_code": "C-02",
                "disposal_method": "incineration",
            },
        )
        self.service.transition(
            self.quar,
            batch["id"],
            "destroy",
            {"method": "incineration", "witnessed_by": "W-9"},
        )
        release = [
            event for event in self.service.location_events()
            if event["event"] == "released"
        ][0]
        self.assertIn("销毁", release["reason"])
        self.assertEqual(release["actor_id"], "quar-1")

    def test_quarantine_requires_registered_cell_and_method(self):
        batch = self._make_inspected("C-11")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.quar,
                batch["id"],
                "quarantine",
                {
                    "pest_found": True,
                    "sample_id": "S-11",
                    "location_code": "GHOST",
                    "disposal_method": "incineration",
                },
            )
        self.service.register_location(self.quar, {"code": "A-01"})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.quar,
                batch["id"],
                "quarantine",
                {
                    "pest_found": True,
                    "sample_id": "S-11",
                    "location_code": "A-01",
                    "disposal_method": "nonsense",
                },
            )

    def test_duplicate_location_registration_conflicts(self):
        self.service.register_location(self.quar, {"code": "A-01"})
        with self.assertRaises(ConflictError):
            self.service.register_location(self.quar, {"code": "A-01"})

    def test_viewer_cannot_register_location(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_location(
                Actor("v", "viewer"), {"code": "A-01"}
            )

    def test_state_survives_restart(self):
        db_path = Path(self.tmp.name) / "persist.db"
        repo = SQLiteRepository(db_path)
        service = DomainService(repo, RuleEngine())
        service.register_location(self.quar, {"code": "A-01"})
        batch = self._make_inspected_with(service, "C-200")
        service.transition(
            self.quar,
            batch["id"],
            "quarantine",
            {
                "pest_found": True,
                "sample_id": "S-200",
                "location_code": "A-01",
                "disposal_method": "incineration",
            },
        )
        del service, repo
        restarted_repo = SQLiteRepository(db_path)
        restarted = DomainService(restarted_repo, RuleEngine())
        location = restarted.list_locations()[0]
        self.assertEqual(location["status"], "occupied")
        self.assertEqual(location["occupancy"]["consignment_code"], "C-200")
        # 重启后重复提交仍然沿用原单
        again = restarted.transition(
            self.quar,
            batch["id"],
            "quarantine",
            {
                "pest_found": True,
                "sample_id": "S-200",
                "location_code": "A-01",
                "disposal_method": "incineration",
            },
        )
        self.assertEqual(again["version"], 3)

    def _make_inspected_with(self, service, code):
        entity = service.create(
            self.admin,
            "consignment",
            {"code": code, "origin": "Port-A", "destination": "Farm-B"},
        )
        return service.transition(
            self.insp,
            entity["id"],
            "inspect",
            {"inspector": "inspector-1", "inspection_result": "suspected"},
        )


if __name__ == "__main__":
    unittest.main()
