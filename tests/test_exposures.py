import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class ExposureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        # quantity 12 >= threshold 10：达到调查水平
        self.item = self.service.create_item({
            "title": "dose event", "description": "over threshold",
            "severity": "high", "quantity": 12, "threshold": 10,
            "external_ref": "EXP-1",
        }, "creator", "dosimetrist")

    def tearDown(self):
        self.repo.close(); self.tmp.cleanup()

    def _to_follow_up(self, item=None):
        current = item or self.item
        for target in STATES[1:4]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def test_single_record_per_employee(self):
        payload = {"employee_no": "E-001", "dose": 12.0,
                   "notify_method": "sms", "follow_up_required": True}
        first = self.service.add_exposure(self.item["id"], payload, "dos", "dosimetrist")
        self.assertEqual(first["employee_no"], "E-001")
        self.assertTrue(first["pending_notification"])
        with self.assertRaises(ConflictError):
            self.service.add_exposure(self.item["id"], payload, "dos", "dosimetrist")

    def test_invalid_notify_method_rejected(self):
        with self.assertRaises((ValidationError, ValueError)):
            self.service.add_exposure(self.item["id"], {
                "employee_no": "E-002", "dose": 1.0,
                "notify_method": "shout", "follow_up_required": False,
            }, "dos", "dosimetrist")

    def test_close_blocked_with_unbooked_follow_up_and_unconfirmed(self):
        self.service.add_exposure(self.item["id"], {
            "employee_no": "E-010", "dose": 12.0,
            "notify_method": "sms", "follow_up_required": True,
        }, "dos", "dosimetrist")
        current = self._to_follow_up()
        try:
            self.service.transition(current["id"], "closed", current["version"],
                                    "hp", "health_physicist")
            self.fail("expected ConflictError")
        except ConflictError as exc:
            joined = " ".join(exc.details)
            self.assertIn("E-010", joined)
            self.assertTrue(any("随访" in d for d in exc.details))
            self.assertTrue(any("调查水平" in d for d in exc.details))
            self.assertEqual(len(exc.details), 2)

    def test_notify_confirm_appointment_then_close_succeeds(self):
        self.service.add_exposure(self.item["id"], {
            "employee_no": "E-020", "dose": 12.0,
            "notify_method": "phone", "follow_up_required": True,
        }, "dos", "dosimetrist")
        exposure = self.service.list_exposures(self.item["id"], "viewer")[0]
        # 未通知先确认应冲突
        with self.assertRaises(ConflictError):
            self.service.confirm_exposure(self.item["id"], exposure["id"],
                                          "ro", "radiation_officer")
        self.service.notify_exposure(self.item["id"], exposure["id"],
                                     "ro", "radiation_officer")
        # 随访角色不能确认以外的越权
        with self.assertRaises(PermissionDenied):
            self.service.notify_exposure(self.item["id"], exposure["id"],
                                         "x", "viewer")
        self.service.confirm_exposure(self.item["id"], exposure["id"],
                                      "ro", "radiation_officer")
        self.service.schedule_follow_up(self.item["id"], exposure["id"], {
            "appointment_at": "2026-10-01T09:00:00+00:00",
        }, "hp", "health_physicist")
        current = self._to_follow_up()
        closed = self.service.transition(current["id"], "closed",
                                         current["version"], "hp",
                                         "health_physicist")
        self.assertEqual(closed["status"], "closed")

    def test_under_threshold_no_follow_up_does_not_block(self):
        self.service.add_exposure(self.item["id"], {
            "employee_no": "E-030", "dose": 2.0,
            "notify_method": "email", "follow_up_required": False,
        }, "dos", "dosimetrist")
        current = self._to_follow_up()
        closed = self.service.transition(current["id"], "closed",
                                         current["version"], "hp",
                                         "health_physicist")
        self.assertEqual(closed["status"], "closed")

    def test_severity_change_voids_confirmation_and_requires_renotify(self):
        self.service.add_exposure(self.item["id"], {
            "employee_no": "E-040", "dose": 12.0,
            "notify_method": "sms", "follow_up_required": False,
        }, "dos", "dosimetrist")
        exposure = self.service.list_exposures(self.item["id"], "viewer")[0]
        self.service.notify_exposure(self.item["id"], exposure["id"],
                                     "ro", "radiation_officer")
        self.service.confirm_exposure(self.item["id"], exposure["id"],
                                      "ro", "radiation_officer")
        updated = self.service.adjust_severity(self.item["id"], {
            "severity": "critical",
        }, "ro", "radiation_officer")
        self.assertEqual(updated["severity"], "critical")
        self.assertEqual(updated["version"], self.item["version"] + 1)
        exposure = self.service.list_exposures(self.item["id"], "viewer")[0]
        self.assertIsNone(exposure["notified_at"])
        self.assertIsNone(exposure["confirmed_at"])
        self.assertTrue(exposure["pending_notification"])
        # 旧确认作废后不能直接再确认
        with self.assertRaises(ConflictError):
            self.service.confirm_exposure(self.item["id"], exposure["id"],
                                          "ro", "radiation_officer")
        # 按新档重新通知后才能确认
        self.service.notify_exposure(self.item["id"], exposure["id"],
                                     "ro", "radiation_officer")
        self.service.confirm_exposure(self.item["id"], exposure["id"],
                                      "ro", "radiation_officer")

    def test_version_conflict_on_severity_adjust(self):
        with self.assertRaises(ConflictError):
            self.service.adjust_severity(self.item["id"], {
                "severity": "critical", "expected_version": 999,
            }, "ro", "radiation_officer")

    def test_roles_for_exposure_endpoints(self):
        with self.assertRaises(PermissionDenied):
            self.service.add_exposure(self.item["id"], {
                "employee_no": "E-050", "dose": 1.0,
                "notify_method": "sms", "follow_up_required": False,
            }, "x", "viewer")
        exposure = self.service.add_exposure(self.item["id"], {
            "employee_no": "E-050", "dose": 1.0,
            "notify_method": "sms", "follow_up_required": True,
        }, "dos", "dosimetrist")
        with self.assertRaises(PermissionDenied):
            self.service.schedule_follow_up(self.item["id"], exposure["id"], {
                "appointment_at": "2026-10-01T09:00:00+00:00",
            }, "ro", "radiation_officer")

    def test_appointment_requires_follow_up_flag(self):
        with self.assertRaises(ValidationError):
            self.service.add_exposure(self.item["id"], {
                "employee_no": "E-060", "dose": 1.0,
                "notify_method": "sms", "follow_up_required": False,
                "appointment_at": "2026-10-01T09:00:00+00:00",
            }, "dos", "dosimetrist")

    def test_toggling_follow_up_off_cancels_appointment(self):
        exposure = self.service.add_exposure(self.item["id"], {
            "employee_no": "E-070", "dose": 1.0,
            "notify_method": "sms", "follow_up_required": True,
            "appointment_at": "2026-10-01T09:00:00+00:00",
        }, "dos", "dosimetrist")
        updated = self.service.update_exposure(self.item["id"], exposure["id"], {
            "follow_up_required": False,
        }, "hp", "health_physicist")
        self.assertFalse(updated["follow_up_required"])
        self.assertIsNone(updated["appointment_at"])

    def test_follow_up_fields_restricted_to_health_physicist(self):
        exposure = self.service.add_exposure(self.item["id"], {
            "employee_no": "E-071", "dose": 1.0,
            "notify_method": "sms", "follow_up_required": False,
        }, "dos", "dosimetrist")
        with self.assertRaises(PermissionDenied):
            self.service.update_exposure(self.item["id"], exposure["id"], {
                "follow_up_required": True,
            }, "ro", "radiation_officer")

    def test_audit_records_all_exposure_actions(self):
        exposure = self.service.add_exposure(self.item["id"], {
            "employee_no": "E-080", "dose": 12.0,
            "notify_method": "sms", "follow_up_required": False,
        }, "dos", "dosimetrist")
        self.service.notify_exposure(self.item["id"], exposure["id"],
                                     "ro", "radiation_officer")
        self.service.confirm_exposure(self.item["id"], exposure["id"],
                                      "ro", "radiation_officer")
        self.service.adjust_severity(self.item["id"], {"severity": "critical"},
                                     "ro", "radiation_officer")
        events = self.service.audit("viewer", self.item["id"])
        actions = [e["action"] for e in events]
        for action in ("exposure_register", "exposure_notify",
                       "exposure_confirm", "severity_adjust", "notify_reset"):
            self.assertIn(action, actions)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_summary_counts_pending(self):
        self.service.add_exposure(self.item["id"], {
            "employee_no": "E-090", "dose": 12.0,
            "notify_method": "sms", "follow_up_required": True,
        }, "dos", "dosimetrist")
        summary = self.service.exposure_summary("viewer")
        row = next(r for r in summary if r["item_id"] == self.item["id"])
        self.assertEqual(row["exposure_count"], 1)
        self.assertEqual(row["pending_notification"], 1)
        self.assertEqual(row["pending_follow_up"], 1)
        self.assertGreaterEqual(row["blocking_count"], 2)
        exposure = self.service.list_exposures(self.item["id"], "viewer")[0]
        self.service.notify_exposure(self.item["id"], exposure["id"],
                                     "ro", "radiation_officer")
        self.service.confirm_exposure(self.item["id"], exposure["id"],
                                      "ro", "radiation_officer")
        self.service.schedule_follow_up(self.item["id"], exposure["id"], {
            "appointment_at": "2026-10-01T09:00:00+00:00",
        }, "hp", "health_physicist")
        row = next(r for r in self.service.exposure_summary("viewer")
                   if r["item_id"] == self.item["id"])
        self.assertEqual(row["pending_notification"], 0)
        self.assertEqual(row["pending_follow_up"], 0)
        self.assertEqual(row["blocking_count"], 0)

    def test_closed_event_rejects_ledger_changes(self):
        self.service.add_exposure(self.item["id"], {
            "employee_no": "E-100", "dose": 1.0,
            "notify_method": "email", "follow_up_required": False,
        }, "dos", "dosimetrist")
        current = self._to_follow_up()
        self.service.transition(current["id"], "closed", current["version"],
                                "hp", "health_physicist")
        with self.assertRaises(ConflictError):
            self.service.add_exposure(self.item["id"], {
                "employee_no": "E-101", "dose": 1.0,
                "notify_method": "email", "follow_up_required": False,
            }, "dos", "dosimetrist")


if __name__ == "__main__":
    unittest.main()
