from __future__ import annotations

import unittest
from unittest import mock

from cairn.dispatcher.contracts import (
    MAX_OUTPUT_FINDINGS,
    extract_findings,
    validate_bootstrap_conclude_payload,
    validate_bootstrap_execute_payload,
    validate_explore_payload,
    validate_findings,
    validate_reason_payload,
)
from cairn.dispatcher.protocol.client import ApiResult, CairnClient
from cairn.dispatcher.tasks.common import best_effort_write_findings
from cairn.server.models import (
    MAX_FINDING_LONG_FIELD_LENGTH,
    MAX_FINDING_SHORT_FIELD_LENGTH,
    MAX_FINDING_TITLE_LENGTH,
)


def _finding(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "title": "Reflected XSS in search",
        "severity": "high",
        "vuln_type": "xss",
        "target": "app.example.com",
        "endpoint": "/search?q=",
    }
    base.update(overrides)
    return base


class ExtractFindingsTests(unittest.TestCase):
    def test_missing_findings_returns_empty_list(self) -> None:
        self.assertEqual(extract_findings({}), [])
        self.assertEqual(extract_findings({"accepted": True, "data": {"description": "d"}}), [])
        self.assertEqual(extract_findings({"description": "legacy unwrapped"}), [])

    def test_wrapped_data_findings_are_normalized(self) -> None:
        payload = {
            "accepted": True,
            "data": {
                "description": "found xss",
                "findings": [_finding(severity="HIGH", status="confirmed", cve="CVE-2024-1")],
            },
        }
        findings = extract_findings(payload)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["title"], "Reflected XSS in search")
        self.assertEqual(findings[0]["severity"], "high")
        self.assertEqual(findings[0]["status"], "open")
        self.assertEqual(findings[0]["vuln_type"], "xss")
        self.assertEqual(findings[0]["target"], "app.example.com")
        self.assertEqual(findings[0]["endpoint"], "/search?q=")
        self.assertEqual(findings[0]["cve"], "CVE-2024-1")

    def test_unwrapped_legacy_payload_findings_are_extracted(self) -> None:
        findings = extract_findings({"description": "d", "findings": [_finding()]})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["title"], "Reflected XSS in search")

    def test_accepted_false_returns_empty_list(self) -> None:
        payload = {"accepted": False, "reason": "policy_refusal", "data": {"findings": [_finding()]}}
        self.assertEqual(extract_findings(payload), [])

    def test_malformed_payload_never_raises(self) -> None:
        self.assertEqual(extract_findings(None), [])  # type: ignore[arg-type]
        self.assertEqual(extract_findings("not-a-dict"), [])  # type: ignore[arg-type]
        self.assertEqual(extract_findings({"accepted": True, "data": []}), [])
        self.assertEqual(extract_findings({"accepted": True, "data": {"findings": "oops"}}), [])
        self.assertEqual(extract_findings({"findings": [{"title": None}]}), [])

    def test_validate_findings_rejects_non_list(self) -> None:
        with self.assertRaises(ValueError):
            validate_findings({"title": "x"})
        with self.assertRaises(ValueError):
            validate_findings("x")


class ValidateFindingsTests(unittest.TestCase):
    def test_unknown_severity_becomes_info_and_case_is_normalized(self) -> None:
        findings = validate_findings(
            [
                _finding(severity="CRITICAL"),
                _finding(title="Open port", severity="High"),
                _finding(title="Weird", severity="totally-unknown"),
                _finding(title="Missing severity", severity=None),
            ]
        )
        self.assertEqual([f["severity"] for f in findings], ["critical", "high", "info", "info"])

    def test_status_is_always_forced_to_open(self) -> None:
        findings = validate_findings(
            [
                _finding(status="confirmed"),
                _finding(title="Fixed bug", status="fixed"),
                _finding(title="No status"),
            ]
        )
        self.assertEqual([f["status"] for f in findings], ["open", "open", "open"])

    def test_items_without_usable_title_are_skipped(self) -> None:
        findings = validate_findings(
            [
                _finding(title=""),
                _finding(title="   "),
                _finding(title=None),
                _finding(title="T" * (MAX_FINDING_TITLE_LENGTH + 1)),
                "not-an-object",
                _finding(title="  kept title  "),
            ]
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["title"], "kept title")

    def test_text_fields_are_truncated_and_unknown_keys_dropped(self) -> None:
        boundary_title = "T" * MAX_FINDING_TITLE_LENGTH
        findings = validate_findings(
            [
                {
                    "title": boundary_title,
                    "severity": "info",
                    "target": "x" * 1000,
                    "vuln_type": "y" * 1000,
                    "evidence": "e" * (MAX_FINDING_LONG_FIELD_LENGTH + 1000),
                    "impact": "i" * (MAX_FINDING_LONG_FIELD_LENGTH + 1),
                    "remediation": "r" * (MAX_FINDING_LONG_FIELD_LENGTH + 1),
                    "fact_id": "f001",
                    "unknown_field": "dropped",
                }
            ]
        )
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["title"], boundary_title)
        self.assertEqual(len(finding["target"]), MAX_FINDING_SHORT_FIELD_LENGTH)
        self.assertEqual(len(finding["vuln_type"]), MAX_FINDING_SHORT_FIELD_LENGTH)
        self.assertEqual(len(finding["evidence"]), MAX_FINDING_LONG_FIELD_LENGTH)
        self.assertEqual(len(finding["impact"]), MAX_FINDING_LONG_FIELD_LENGTH)
        self.assertEqual(len(finding["remediation"]), MAX_FINDING_LONG_FIELD_LENGTH)
        self.assertEqual(finding["fact_id"], "f001")
        self.assertNotIn("unknown_field", finding)

    def test_fact_id_only_kept_as_non_empty_string(self) -> None:
        findings = validate_findings(
            [
                _finding(title="A", fact_id="  f001  "),
                _finding(title="B", fact_id=123),
                _finding(title="C", fact_id="   "),
            ]
        )
        self.assertEqual([f["fact_id"] for f in findings], ["f001", None, None])

    def test_duplicate_findings_are_deduplicated_by_fingerprint(self) -> None:
        findings = validate_findings(
            [
                _finding(title="SQL Injection", target="App.Example.com", vuln_type="sqli"),
                _finding(title="sql   injection", target="app.example.com ", vuln_type="SQLI"),
                _finding(title="SQL Injection", target="other.example.com", vuln_type="sqli"),
            ]
        )
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0]["title"], "SQL Injection")

    def test_output_is_capped_at_fifty_findings(self) -> None:
        raw = [_finding(title=f"vuln {index}") for index in range(MAX_OUTPUT_FINDINGS + 10)]
        findings = validate_findings(raw)
        self.assertEqual(len(findings), MAX_OUTPUT_FINDINGS)
        self.assertEqual(findings[-1]["title"], f"vuln {MAX_OUTPUT_FINDINGS - 1}")


class ValidatorCompatibilityTests(unittest.TestCase):
    def test_validators_accept_legacy_output_without_findings(self) -> None:
        self.assertEqual(
            validate_reason_payload(
                {"accepted": True, "data": {"complete": {"from": ["f001"], "description": "done"}}},
                open_intents_empty=False,
                max_intents=3,
            )[0],
            "complete",
        )
        self.assertEqual(
            validate_bootstrap_execute_payload(
                {"accepted": True, "data": {"fact": {"description": "f"}, "complete": {"description": "c"}}}
            )[0],
            "complete",
        )
        self.assertEqual(
            validate_bootstrap_conclude_payload({"accepted": True, "data": {"fact": {"description": "f"}}})[0],
            "fact",
        )
        self.assertEqual(
            validate_explore_payload({"accepted": True, "data": {"description": "d"}})[0],
            "fact",
        )

    def test_validators_accept_output_with_optional_findings(self) -> None:
        findings = [_finding()]

        self.assertEqual(
            validate_reason_payload(
                {
                    "accepted": True,
                    "data": {
                        "complete": {"from": ["f001"], "description": "done"},
                        "findings": findings,
                    },
                },
                open_intents_empty=False,
                max_intents=3,
            )[0],
            "complete",
        )
        self.assertEqual(
            validate_reason_payload(
                {"accepted": True, "data": {"intents": [{"from": ["f001"], "description": "next"}], "findings": findings}},
                open_intents_empty=False,
                max_intents=3,
            )[0],
            "intents",
        )
        self.assertEqual(
            validate_bootstrap_execute_payload(
                {
                    "accepted": True,
                    "data": {"fact": {"description": "f"}, "complete": {"description": "c"}, "findings": findings},
                }
            )[0],
            "complete",
        )
        self.assertEqual(
            validate_bootstrap_conclude_payload(
                {"accepted": True, "data": {"fact": {"description": "f"}, "findings": findings}}
            )[0],
            "fact",
        )
        self.assertEqual(
            validate_explore_payload({"accepted": True, "data": {"description": "d", "findings": findings}})[0],
            "fact",
        )

    def test_legacy_unwrapped_output_still_accepts_findings(self) -> None:
        findings = [_finding()]
        self.assertEqual(
            validate_reason_payload(
                {"complete": {"from": ["f001"], "description": "done"}, "findings": findings},
                open_intents_empty=False,
                max_intents=3,
            )[0],
            "complete",
        )
        self.assertEqual(
            validate_explore_payload({"description": "d", "findings": findings})[0],
            "fact",
        )

    def test_bootstrap_conclude_still_rejects_unknown_extra_keys(self) -> None:
        with self.assertRaises(ValueError):
            validate_bootstrap_conclude_payload(
                {"accepted": True, "data": {"fact": {"description": "f"}, "other": 1}}
            )


class _FakeClient:
    def __init__(self, result: ApiResult | None = None, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, list[dict[str, object]], str | None]] = []
        self._result = result
        self._error = error

    def create_findings(
        self,
        project_id: str,
        findings: list[dict[str, object]],
        worker: str | None = None,
    ) -> ApiResult:
        self.calls.append((project_id, findings, worker))
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class BestEffortWriteFindingsTests(unittest.TestCase):
    def test_successful_write_passes_expected_arguments(self) -> None:
        fake = _FakeClient(result=ApiResult(200, {"created": 1, "updated": 0}, ""))
        payload = {"accepted": True, "data": {"description": "d", "findings": [_finding()]}}
        written = best_effort_write_findings(fake, "p1", payload, "worker-1", source="unit_test")
        self.assertEqual(written, 1)
        self.assertEqual(len(fake.calls), 1)
        project_id, findings, worker = fake.calls[0]
        self.assertEqual(project_id, "p1")
        self.assertEqual(worker, "worker-1")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["title"], "Reflected XSS in search")
        self.assertEqual(findings[0]["status"], "open")

    def test_payload_without_findings_does_not_call_client(self) -> None:
        fake = _FakeClient(result=ApiResult(200, {}, ""))
        self.assertEqual(best_effort_write_findings(fake, "p1", {"description": "d"}, "worker-1", source="s"), 0)
        self.assertEqual(fake.calls, [])

    def test_client_exception_is_swallowed(self) -> None:
        fake = _FakeClient(error=RuntimeError("connection refused"))
        payload = {"accepted": True, "data": {"description": "d", "findings": [_finding()]}}
        self.assertEqual(best_effort_write_findings(fake, "p1", payload, "worker-1", source="s"), 0)

    def test_error_statuses_do_not_raise(self) -> None:
        payload = {"accepted": True, "data": {"description": "d", "findings": [_finding()]}}
        for status in (403, 500):
            with self.subTest(status=status):
                fake = _FakeClient(result=ApiResult(status, {"detail": "nope"}, "error body"))
                self.assertEqual(best_effort_write_findings(fake, "p1", payload, "worker-1", source="s"), 1)


class ClientCreateFindingsTests(unittest.TestCase):
    def test_create_findings_posts_expected_body(self) -> None:
        expected = ApiResult(200, {"created": 1, "updated": 0}, "")
        with mock.patch.object(CairnClient, "_request_json", return_value=expected) as request:
            client = CairnClient("http://example.invalid")
            findings = [{"title": "x"}]
            result = client.create_findings("p1", findings, "worker-1")
        self.assertIs(result, expected)
        request.assert_called_once_with(
            "POST",
            "/projects/p1/findings",
            json={"findings": findings, "worker": "worker-1"},
        )


if __name__ == "__main__":
    unittest.main()
