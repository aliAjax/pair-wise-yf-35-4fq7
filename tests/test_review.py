import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _date(offset_days):
    return (datetime.now(timezone.utc).date() + timedelta(days=offset_days)).isoformat()


class ReviewFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.panel = Actor("panel-1", "panel")
        self.lab = Actor("lab-1", "lab")
        self.athlete = Actor("athlete-1", "athlete")

    def tearDown(self):
        self.tmp.cleanup()

    def _open_case(self, deadline=None):
        athlete = self.service.create(
            self.admin, "athlete", {"name": "A. Rider", "discipline": "cycling"}
        )
        sample = self.service.create(
            self.admin,
            "sample",
            {"athlete_id": athlete["id"], "sample_code": "S-100", "event": "final"},
        )
        for action, data in (
            ("collect", {"collected_at": "2026-01-01T08:00:00Z"}),
            ("seal", {"seal_id": "SEAL-1"}),
            ("ship", {"carrier": "Courier-A"}),
            ("receive", {"lab_id": "LAB-1"}),
            ("analyze", {"result": "adverse"}),
            ("report_adverse", {}),
        ):
            sample = self.service.transition(self.admin, sample["id"], action, data)
        case_data = {
            "athlete_id": athlete["id"],
            "sample_id": sample["id"],
            "alleged_rule": "substance-1",
        }
        if deadline:
            case_data["review_deadline"] = deadline
        case = self.service.create(self.admin, "case", case_data)
        return sample, case

    def _suspended_case(self, deadline=None):
        sample, case = self._open_case(deadline)
        case = self.service.transition(
            self.panel, case["id"], "provisional_suspend", {"reason": "adverse A sample"}
        )
        return sample, case

    def test_case_snapshots_initial_result_and_retention_deadline(self):
        _, case = self._open_case()
        self.assertEqual(case["data"]["initial_result"], "adverse")
        self.assertEqual(case["data"]["review_deadline"], _date(30))

    def test_confirmed_review_continues_suspension(self):
        _, case = self._suspended_case()
        case = self.service.transition(self.athlete, case["id"], "request_review", {})
        self.assertEqual(case["status"], "review_pending")
        self.assertEqual(case["data"]["review_requested_by"], "athlete-1")
        case = self.service.transition(
            self.lab, case["id"], "report_review", {"review_result": "confirmed"}
        )
        self.assertEqual(case["status"], "suspended")
        self.assertEqual(case["data"]["review_result"], "confirmed")
        self.assertTrue(case["data"]["review_consistent"])
        # 初检与复检结论都留在案件里
        self.assertEqual(case["data"]["initial_result"], "adverse")
        # 临时禁赛继续，后续流程照常推进
        case = self.service.transition(
            self.panel, case["id"], "schedule_hearing", {"hearing_at": "2026-03-01"}
        )
        case = self.service.transition(
            self.panel, case["id"], "decide", {"decision": "sanction"}
        )
        self.assertEqual(case["status"], "closed")

    def test_negative_review_is_resolved_with_no_sanction(self):
        _, case = self._suspended_case()
        case = self.service.transition(self.athlete, case["id"], "request_review", {})
        case = self.service.transition(
            self.lab, case["id"], "report_review", {"review_result": "negative"}
        )
        self.assertEqual(case["status"], "review_disputed")
        self.assertEqual(case["data"]["initial_result"], "adverse")
        self.assertEqual(case["data"]["review_result"], "negative")
        self.assertFalse(case["data"]["review_consistent"])
        # 结论不一致只能按无处罚结束
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.panel, case["id"], "resolve_review", {"decision": "sanction"}
            )
        case = self.service.transition(
            self.panel, case["id"], "resolve_review", {"decision": "no_sanction"}
        )
        self.assertEqual(case["status"], "closed")
        self.assertEqual(case["data"]["decision"], "no_sanction")
        self.assertEqual(case["data"]["decided_by"], "panel-1")

    def test_retention_expired_ends_without_sanction(self):
        _, case = self._suspended_case(deadline=_date(-1))
        # 保留期已过，不能再提出复核
        with self.assertRaises(ValidationError):
            self.service.transition(self.athlete, case["id"], "request_review", {})
        case = self.service.transition(self.panel, case["id"], "expire_review", {})
        self.assertEqual(case["status"], "closed")
        self.assertEqual(case["data"]["decision"], "no_sanction")
        self.assertEqual(case["data"]["closure"], "retention_expired")

    def test_expire_review_requires_passed_deadline(self):
        _, case = self._suspended_case()
        with self.assertRaises(ValidationError):
            self.service.transition(self.panel, case["id"], "expire_review", {})

    def test_confirmed_review_cannot_expire_or_be_requested_again(self):
        _, case = self._suspended_case(deadline=_date(0))
        case = self.service.transition(self.athlete, case["id"], "request_review", {})
        case = self.service.transition(
            self.lab, case["id"], "report_review", {"review_result": "confirmed"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(self.panel, case["id"], "expire_review", {})
        with self.assertRaises(ValidationError):
            self.service.transition(self.athlete, case["id"], "request_review", {})

    def test_review_permissions(self):
        _, case = self._suspended_case()
        with self.assertRaises(PermissionDenied):
            self.service.transition(Actor("v", "viewer"), case["id"], "request_review", {})
        case = self.service.transition(self.athlete, case["id"], "request_review", {})
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.athlete, case["id"], "report_review", {"review_result": "confirmed"}
            )
        case = self.service.transition(
            self.lab, case["id"], "report_review", {"review_result": "negative"}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.lab, case["id"], "resolve_review", {"decision": "no_sanction"}
            )

    def test_ordinary_update_cannot_overwrite_conclusions(self):
        # 样本层面：已记录的 result 不能被后续动作改写
        athlete = self.service.create(
            self.admin, "athlete", {"name": "B. Rider", "discipline": "cycling"}
        )
        sample = self.service.create(
            self.admin,
            "sample",
            {"athlete_id": athlete["id"], "sample_code": "S-200", "event": "final"},
        )
        for action, data in (
            ("collect", {"collected_at": "2026-01-01T08:00:00Z"}),
            ("seal", {"seal_id": "SEAL-2"}),
            ("ship", {"carrier": "Courier-A"}),
            ("receive", {"lab_id": "LAB-1"}),
            ("analyze", {"result": "adverse"}),
        ):
            sample = self.service.transition(self.admin, sample["id"], action, data)
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.lab, sample["id"], "clear", {"reason": "dup", "result": "cleared"}
            )
        # 案件层面：复核结论不能被后续普通更新覆盖
        _, case = self._suspended_case()
        case = self.service.transition(self.athlete, case["id"], "request_review", {})
        case = self.service.transition(
            self.lab, case["id"], "report_review", {"review_result": "negative"}
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.panel,
                case["id"],
                "resolve_review",
                {"decision": "no_sanction", "review_result": "confirmed"},
            )
        # 初检结论同样受保护
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.panel,
                case["id"],
                "resolve_review",
                {"decision": "no_sanction", "initial_result": "negative"},
            )
        # 不带结论字段的正常处置不受影响
        case = self.service.transition(
            self.panel, case["id"], "resolve_review", {"decision": "no_sanction"}
        )
        self.assertEqual(case["status"], "closed")

    def test_same_value_conclusion_update_is_allowed(self):
        rules = RuleEngine()
        entity = {
            "kind": "case",
            "status": "suspended",
            "data": {"initial_result": "adverse", "review_result": "confirmed"},
        }
        status, patch = rules.validate_transition(
            self.panel,
            entity,
            "schedule_hearing",
            {"hearing_at": "2026-03-01", "review_result": "confirmed"},
        )
        self.assertEqual(status, "hearing")
        with self.assertRaises(ConflictError):
            rules.validate_transition(
                self.panel,
                entity,
                "schedule_hearing",
                {"hearing_at": "2026-03-01", "review_result": "negative"},
            )


if __name__ == "__main__":
    unittest.main()
