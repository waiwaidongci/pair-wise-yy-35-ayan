import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class ExposureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "dose event", "description": "exposure ledger test",
             "severity": "high", "quantity": 12, "threshold": 6,
             "external_ref": "EXP-1"}, "creator", "dosimetrist")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _register(self, employee_id="E001", dose=8.0, follow_up=True, method="phone"):
        return self.service.register_exposure(
            self.item["id"],
            {"employee_id": employee_id, "dose": dose,
             "notification_method": method, "follow_up_required": follow_up},
            "clerk", "dosimetrist")

    def _advance_to_follow_up(self):
        current = self.service.get_item(self.item["id"], "viewer")
        for target in STATES[1:-1]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def test_register_unique_per_employee(self):
        exposure = self._register()
        self.assertEqual(exposure["employee_id"], "E001")
        self.assertEqual(exposure["dose"], 8.0)
        self.assertEqual(exposure["notification_method"], "phone")
        self.assertTrue(exposure["follow_up_required"])
        self.assertFalse(exposure["notified"])
        self.assertFalse(exposure["confirmed"])
        with self.assertRaises(ConflictError):
            self._register()
        self._register("E002", dose=2.0, follow_up=False, method="email")
        exposures = self.service.list_exposures(self.item["id"], "viewer")
        self.assertEqual(len(exposures), 2)

    def test_close_blocked_until_followup_and_confirmation_done(self):
        self._register("E001", dose=8.0, follow_up=True)
        self._register("E002", dose=2.0, follow_up=False)
        current = self._advance_to_follow_up()
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], "closed", current["version"],
                                    "reviewer", TRANSITION_ROLES["closed"][0])
        self.assertEqual(len(ctx.exception.blockers), 2)
        message = str(ctx.exception)
        self.assertIn("E001", message)
        self.assertIn("随访", message)
        self.assertIn("确认", message)
        self.assertNotIn("E002", message)
        self.service.schedule_follow_up(
            self.item["id"], "E001",
            {"appointment": "2026-10-01T09:00:00+00:00"}, "hp", "health_physicist")
        current = self.service.get_item(self.item["id"], "viewer")
        with self.assertRaises(ConflictError) as ctx:
            self.service.transition(current["id"], "closed", current["version"],
                                    "reviewer", TRANSITION_ROLES["closed"][0])
        self.assertEqual(len(ctx.exception.blockers), 1)
        self.service.confirm_exposure(self.item["id"], "E001", "hp", "health_physicist")
        current = self.service.get_item(self.item["id"], "viewer")
        closed = self.service.transition(current["id"], "closed", current["version"],
                                         "reviewer", TRANSITION_ROLES["closed"][0])
        self.assertEqual(closed["status"], "closed")

    def test_severity_adjust_voids_confirmation_and_requires_renotify(self):
        self._register("E001", dose=8.0, follow_up=False)
        self.service.notify_exposure(self.item["id"], "E001", "officer", "radiation_officer")
        self.service.confirm_exposure(self.item["id"], "E001", "hp", "health_physicist")
        current = self.service.get_item(self.item["id"], "viewer")
        with self.assertRaises(ConflictError):
            self.service.adjust_severity(
                self.item["id"], {"severity": "critical", "expected_version": 99},
                "officer", "radiation_officer")
        updated = self.service.adjust_severity(
            self.item["id"],
            {"severity": "critical", "expected_version": current["version"]},
            "officer", "radiation_officer")
        self.assertEqual(updated["severity"], "critical")
        exposure = self.service.list_exposures(self.item["id"], "viewer")[0]
        self.assertFalse(exposure["confirmed"])
        self.assertFalse(exposure["notified"])
        self.service.notify_exposure(self.item["id"], "E001", "officer", "radiation_officer")
        self.service.confirm_exposure(self.item["id"], "E001", "hp", "health_physicist")
        exposure = self.service.list_exposures(self.item["id"], "viewer")[0]
        self.assertTrue(exposure["confirmed"])
        self.assertTrue(exposure["notified"])
        actions = [e["action"] for e in self.service.audit("viewer", self.item["id"])]
        self.assertIn("severity_adjust", actions)
        self.assertEqual(actions.count("exposure_notify"), 2)
        self.assertEqual(actions.count("exposure_confirm"), 2)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_audit_and_summary(self):
        self._register("E001", dose=8.0, follow_up=True)
        self._register("E002", dose=1.0, follow_up=False)
        summary = self.service.exposure_summary("viewer")
        self.assertEqual(summary["pending_notifications"], 2)
        self.assertEqual(summary["pending_followups"], 1)
        self.service.notify_exposure(self.item["id"], "E001", "officer", "radiation_officer")
        self.service.schedule_follow_up(
            self.item["id"], "E001", {"appointment": "2026-10-01T09:00:00Z"},
            "hp", "health_physicist")
        summary = self.service.exposure_summary("viewer")
        self.assertEqual(summary["pending_notifications"], 1)
        self.assertEqual(summary["pending_followups"], 0)
        actions = [e["action"] for e in self.service.audit("viewer", self.item["id"])]
        for expected in ("exposure_register", "exposure_notify", "exposure_follow_up"):
            self.assertIn(expected, actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_permissions_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_exposure(
                self.item["id"],
                {"employee_id": "E9", "dose": 1, "notification_method": "email",
                 "follow_up_required": False}, "x", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.notify_exposure(self.item["id"], "E1", "x", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.adjust_severity(
                self.item["id"], {"severity": "low", "expected_version": 1},
                "x", "dosimetrist")
        with self.assertRaises(ValidationError):
            self.service.register_exposure(
                self.item["id"],
                {"employee_id": "E9", "dose": -1, "notification_method": "email"},
                "x", "dosimetrist")
        with self.assertRaises(ValidationError):
            self.service.register_exposure(
                self.item["id"],
                {"employee_id": "E9", "dose": 1, "notification_method": "email",
                 "follow_up_required": "yes"}, "x", "dosimetrist")
        self._register("E001", dose=8.0, follow_up=True)
        with self.assertRaises(ValidationError):
            self.service.schedule_follow_up(
                self.item["id"], "E001", {"appointment": "not-a-time"},
                "hp", "health_physicist")
        with self.assertRaises(NotFoundError):
            self.service.confirm_exposure(self.item["id"], "NOPE", "hp", "health_physicist")
        self.service.notify_exposure(self.item["id"], "E001", "officer", "radiation_officer")
        with self.assertRaises(ConflictError):
            self.service.notify_exposure(self.item["id"], "E001", "officer", "radiation_officer")
        self.service.confirm_exposure(self.item["id"], "E001", "hp", "health_physicist")
        with self.assertRaises(ConflictError):
            self.service.confirm_exposure(self.item["id"], "E001", "hp", "health_physicist")
        current = self.service.get_item(self.item["id"], "viewer")
        with self.assertRaises(ValidationError):
            self.service.adjust_severity(
                self.item["id"],
                {"severity": "high", "expected_version": current["version"]},
                "officer", "radiation_officer")


if __name__ == "__main__":
    unittest.main()
