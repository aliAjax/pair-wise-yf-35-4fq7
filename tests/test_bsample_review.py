import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class BSampleReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.athlete = Actor("runner-1", "athlete")
        self.lab = Actor("lab-1", "lab")
        self.panel = Actor("panel-1", "panel")

    def tearDown(self):
        self.tmp.cleanup()

    def _suspended_case(self, filed_at="2026-03-01"):
        self.service.create(
            self.admin, "athlete", {"name": "A. Runner", "discipline": "athletics"}
        )
        athlete = self.service.list("athlete")[0]["id"]
        sample = self.service.create(
            self.admin,
            "sample",
            {"athlete_id": athlete, "sample_code": "S-B1", "event": "open-meet"},
        )["id"]
        for action, data in (
            ("collect", {"collected_at": "2026-03-01T08:00:00Z"}),
            ("seal", {"seal_id": "SEAL-B1"}),
            ("ship", {"carrier": "Courier-B"}),
            ("receive", {"lab_id": "LAB-1"}),
            ("analyze", {"result": "adverse"}),
            ("report_adverse", {}),
        ):
            self.service.transition(
                self.lab if action in ("receive", "analyze", "report_adverse") else self.admin,
                sample,
                action,
                data,
            )
        case = self.service.create(
            self.panel,
            "case",
            {
                "athlete_id": athlete,
                "sample_id": sample,
                "alleged_rule": "substance-9",
                "filed_at": filed_at,
            },
        )["id"]
        self.service.transition(
            self.panel, case, "provisional_suspend", {"reason": "adverse A sample"}
        )
        return case

    def test_not_detected_goes_to_case_manager_and_closes_no_sanction(self):
        case = self._suspended_case()
        self.service.transition(
            self.athlete, case, "request_review", {"requested_at": "2026-03-05"}
        )
        conflict = self.service.transition(
            self.lab,
            case,
            "record_review",
            {"review_result": "not_detected", "lab_report_id": "B-REPORT-2"},
        )
        self.assertEqual(conflict["status"], "review_conflict")
        # 初检与复核两份结论都保留在案件里。
        self.assertEqual(conflict["data"]["initial_result"], "positive")
        self.assertEqual(conflict["data"]["review_result"], "not_detected")
        # 不一致状态不能直接走听证/决定流程。
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.panel, case, "schedule_hearing", {"hearing_at": "2026-04-01"}
            )
        closed = self.service.transition(
            self.panel, case, "redispose", {"decision": "no_sanction"}
        )
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["data"]["decision"], "no_sanction")

    def test_review_request_after_retention_period_rejected(self):
        case = self._suspended_case()  # retention_until = 2026-03-15
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.athlete, case, "request_review", {"requested_at": "2026-03-16"}
            )

    def test_retention_lapsed_closes_with_no_sanction(self):
        case = self._suspended_case()
        closed = self.service.transition(self.panel, case, "lapse_retention", {})
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["data"]["decision"], "no_sanction")
        self.assertNotIn("review_result", closed["data"])

    def test_lapse_after_review_recorded_rejected(self):
        case = self._suspended_case()
        self.service.transition(
            self.athlete, case, "request_review", {"requested_at": "2026-03-05"}
        )
        self.service.transition(
            self.lab,
            case,
            "record_review",
            {"review_result": "positive", "lab_report_id": "B-REPORT-3"},
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.panel, case, "lapse_retention", {})

    def test_lab_cannot_record_review_without_request(self):
        case = self._suspended_case()
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.lab,
                case,
                "record_review",
                {"review_result": "positive", "lab_report_id": "B-REPORT-4"},
            )

    def test_ordinary_update_cannot_overwrite_conclusions(self):
        case = self._suspended_case()
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.panel,
                case,
                "schedule_hearing",
                {"hearing_at": "2026-04-01", "initial_result": "not_detected"},
            )
        self.service.transition(
            self.athlete, case, "request_review", {"requested_at": "2026-03-05"}
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.lab,
                case,
                "record_review",
                {"review_result": "positive", "lab_report_id": "B-5", "initial_result": "x"},
            )

    def test_lab_role_required_for_review_result(self):
        case = self._suspended_case()
        self.service.transition(
            self.athlete, case, "request_review", {"requested_at": "2026-03-05"}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.athlete,
                case,
                "record_review",
                {"review_result": "positive", "lab_report_id": "B-REPORT-6"},
            )

    def test_athlete_role_can_request_review(self):
        case = self._suspended_case()
        pending = self.service.transition(
            self.athlete, case, "request_review", {"requested_at": "2026-03-10"}
        )
        self.assertEqual(pending["status"], "review_pending")
        self.assertEqual(pending["data"]["requested_by"], "runner-1")


if __name__ == "__main__":
    unittest.main()
