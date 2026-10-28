from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

USERNAME = "admin"
PASSWORD = "correct-horse-battery"
TOKEN = "cairn_test_token_bbbbbbbbbbbbbbbbbb"


def _prepare_env(root: Path) -> None:
    os.environ["CAIRN_AUTH_USERNAME"] = USERNAME
    os.environ["CAIRN_AUTH_PASSWORD"] = PASSWORD
    os.environ["CAIRN_API_TOKEN"] = TOKEN
    os.environ["CAIRN_CREDENTIALS_FILE"] = str(root / "credentials")
    os.environ["CAIRN_DB_PATH"] = str(root / "cairn.db")
    os.environ.pop("CAIRN_TRUST_PROXY", None)


def _finding(**overrides) -> dict:
    payload = {
        "title": "SQL injection in login",
        "severity": "high",
        "vuln_type": "sqli",
        "target": "example.com",
        "endpoint": "https://example.com/login",
        "evidence": "payload ' OR 1=1-- bypasses the login form",
    }
    payload.update(overrides)
    return payload


class FindingsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        _prepare_env(root)
        from cairn.server import auth, db

        db.reset()
        db.configure(root / "cairn.db")
        auth.bootstrap_auth()
        auth.reset_login_attempts()
        from cairn.server.app import app

        self.auth = auth
        self.db = db
        self.client = TestClient(app)
        self.project_id = self._create_project()

    def tearDown(self) -> None:
        self.client.close()
        self.db.reset()
        self._tmp.cleanup()

    # helpers ---------------------------------------------------------------

    def _bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {TOKEN}"}

    def _create_project(self, title: str = "Test project") -> str:
        response = self.client.post(
            "/projects",
            json={"title": title, "origin": "origin fact", "goal": "goal fact"},
            headers=self._bearer(),
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["project"]["id"]

    def _post_findings(self, findings: list[dict], project_id: str | None = None):
        return self.client.post(
            f"/projects/{project_id or self.project_id}/findings",
            json={"findings": findings},
            headers=self._bearer(),
        )

    def _get_findings(self, project_id: str | None = None):
        return self.client.get(
            f"/projects/{project_id or self.project_id}/findings",
            headers=self._bearer(),
        )

    def _patch_finding(
        self, finding_id: str, payload: dict, project_id: str | None = None
    ):
        return self.client.patch(
            f"/projects/{project_id or self.project_id}/findings/{finding_id}",
            json=payload,
            headers=self._bearer(),
        )

    def _delete_finding(self, finding_id: str, project_id: str | None = None):
        return self.client.delete(
            f"/projects/{project_id or self.project_id}/findings/{finding_id}",
            headers=self._bearer(),
        )

    # list / summary --------------------------------------------------------

    def test_list_empty_project_has_zero_summary(self) -> None:
        response = self._get_findings()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["findings"], [])
        self.assertEqual(
            body["summary"],
            {
                "total": 0,
                "critical": 0,
                "high": 0,
                "medium": 0,
                "low": 0,
                "info": 0,
                "open": 0,
                "confirmed": 0,
                "fixed": 0,
                "false_positive": 0,
            },
        )

    # create / upsert -------------------------------------------------------

    def test_create_batch_assigns_scoped_ids_and_keeps_fact_id(self) -> None:
        response = self._post_findings(
            [
                _finding(title="SQL injection in login"),
                _finding(title="Open redirect", target="api.example.com", severity="medium"),
                _finding(title="Debug endpoint leaks config", fact_id="goal"),
            ]
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["created"], 3)
        self.assertEqual(body["updated"], 0)
        self.assertEqual([item["id"] for item in body["findings"]], ["v001", "v002", "v003"])
        self.assertEqual(body["findings"][2]["fact_id"], "goal")
        self.assertEqual(body["findings"][2]["status"], "open")

        listed = self._get_findings().json()
        self.assertEqual(listed["summary"]["total"], 3)
        self.assertEqual(listed["summary"]["high"], 2)
        self.assertEqual(listed["summary"]["medium"], 1)
        self.assertEqual(listed["summary"]["critical"], 0)

    def test_upsert_same_fingerprint_updates_instead_of_duplicating(self) -> None:
        first = self._post_findings([_finding()])
        self.assertEqual(first.status_code, 201, first.text)
        finding_id = first.json()["findings"][0]["id"]

        # Same title/target/endpoint/vuln_type modulo case and whitespace.
        second = self._post_findings(
            [
                _finding(
                    title="  sql   INJECTION in login ",
                    severity="critical",
                    evidence="confirmed with sqlmap",
                )
            ]
        )
        self.assertEqual(second.status_code, 201, second.text)
        body = second.json()
        self.assertEqual(body["created"], 0)
        self.assertEqual(body["updated"], 1)
        self.assertEqual([item["id"] for item in body["findings"]], [finding_id])
        self.assertEqual(body["findings"][0]["severity"], "critical")
        self.assertEqual(body["findings"][0]["evidence"], "confirmed with sqlmap")

        listed = self._get_findings().json()
        self.assertEqual(listed["summary"]["total"], 1)

    def test_duplicate_inside_one_batch_updates_same_row(self) -> None:
        response = self._post_findings(
            [_finding(), _finding(title="SQL injection in LOGIN")]
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["created"], 1)
        self.assertEqual(body["updated"], 1)
        self.assertEqual(len(body["findings"]), 1)
        self.assertEqual(body["findings"][0]["id"], "v001")

    def test_agent_open_does_not_override_human_status(self) -> None:
        created = self._post_findings([_finding()])
        self.assertEqual(created.status_code, 201, created.text)
        finding_id = created.json()["findings"][0]["id"]

        patched = self._patch_finding(finding_id, {"status": "confirmed"})
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(patched.json()["status"], "confirmed")

        # Agent re-reports the same finding; input status stays the default open.
        again = self._post_findings([_finding(evidence="re-confirmed")])
        self.assertEqual(again.status_code, 201, again.text)
        body = again.json()
        self.assertEqual(body["created"], 0)
        self.assertEqual(body["updated"], 1)
        self.assertEqual(body["findings"][0]["status"], "confirmed")
        self.assertEqual(body["findings"][0]["evidence"], "re-confirmed")

    # validation ------------------------------------------------------------

    def test_invalid_finding_fields_are_rejected_with_422(self) -> None:
        bad_payloads = [
            _finding(severity="catastrophic"),
            _finding(status="wontfix"),
            _finding(title="x" * 301),
            _finding(target="x" * 513),
            _finding(evidence="x" * 4001),
            {"severity": "high"},  # missing title
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                response = self._post_findings([payload])
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self._get_findings().json()["summary"]["total"], 0)

    def test_findings_array_bounds_are_enforced(self) -> None:
        empty = self._post_findings([])
        self.assertEqual(empty.status_code, 422, empty.text)

        too_many = self._post_findings(
            [_finding(title=f"finding {index}") for index in range(101)]
        )
        self.assertEqual(too_many.status_code, 422, too_many.text)

    # project / fact references --------------------------------------------

    def test_unknown_fact_id_returns_404_on_create_and_patch(self) -> None:
        response = self._post_findings([_finding(fact_id="f999")])
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["detail"], "Fact f999 not found")

        created = self._post_findings([_finding()])
        finding_id = created.json()["findings"][0]["id"]
        patch_response = self._patch_finding(finding_id, {"fact_id": "f999"})
        self.assertEqual(patch_response.status_code, 404, patch_response.text)
        self.assertEqual(
            self._get_findings().json()["findings"][0]["fact_id"], None
        )

    def test_unknown_project_returns_404_on_every_route(self) -> None:
        missing = "proj_999"
        self.assertEqual(
            self.client.get(f"/projects/{missing}/findings", headers=self._bearer()).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f"/projects/{missing}/findings/export", headers=self._bearer()
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/projects/{missing}/findings",
                json={"findings": [_finding()]},
                headers=self._bearer(),
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.patch(
                f"/projects/{missing}/findings/v001",
                json={"status": "fixed"},
                headers=self._bearer(),
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(
                f"/projects/{missing}/findings/v001", headers=self._bearer()
            ).status_code,
            404,
        )

    # ordering --------------------------------------------------------------

    def test_list_sorted_by_severity_rank(self) -> None:
        response = self._post_findings(
            [
                _finding(title="I", severity="info"),
                _finding(title="C", severity="critical"),
                _finding(title="L", severity="low"),
                _finding(title="H", severity="high"),
                _finding(title="M", severity="medium"),
            ]
        )
        self.assertEqual(response.status_code, 201, response.text)

        listed = self._get_findings().json()
        self.assertEqual(
            [item["severity"] for item in listed["findings"]],
            ["critical", "high", "medium", "low", "info"],
        )
        self.assertEqual(
            [item["id"] for item in listed["findings"]],
            ["v002", "v004", "v005", "v003", "v001"],
        )
        self.assertEqual(listed["summary"]["total"], 5)

    # patch -----------------------------------------------------------------

    def test_patch_only_changes_requested_field_and_refreshes_updated_at(self) -> None:
        created = self._post_findings([_finding()])
        finding_id = created.json()["findings"][0]["id"]
        with self.db.get_conn() as conn:
            conn.execute(
                "UPDATE findings SET updated_at = '2020-01-01T00:00:00Z' "
                "WHERE id = ? AND project_id = ?",
                (finding_id, self.project_id),
            )

        response = self._patch_finding(finding_id, {"status": "fixed"})
        self.assertEqual(response.status_code, 200, response.text)
        updated = response.json()
        self.assertEqual(updated["status"], "fixed")
        self.assertEqual(updated["title"], "SQL injection in login")
        self.assertEqual(updated["severity"], "high")
        self.assertEqual(updated["target"], "example.com")
        self.assertEqual(updated["endpoint"], "https://example.com/login")
        self.assertEqual(updated["evidence"], "payload ' OR 1=1-- bypasses the login form")
        self.assertNotEqual(updated["updated_at"], "2020-01-01T00:00:00Z")
        # created_at must be untouched by the patch.
        self.assertEqual(updated["created_at"], created.json()["findings"][0]["created_at"])

        # An empty PATCH body returns the current row unchanged.
        empty = self._patch_finding(finding_id, {})
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json(), updated)

    def test_patch_unknown_finding_returns_404(self) -> None:
        response = self._patch_finding("v999", {"status": "fixed"})
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["detail"], "Finding not found")

    # delete ----------------------------------------------------------------

    def test_delete_returns_204_then_404(self) -> None:
        created = self._post_findings([_finding()])
        finding_id = created.json()["findings"][0]["id"]

        first = self._delete_finding(finding_id)
        self.assertEqual(first.status_code, 204, first.text)
        self.assertEqual(self._get_findings().json()["summary"]["total"], 0)

        second = self._delete_finding(finding_id)
        self.assertEqual(second.status_code, 404, second.text)
        self.assertEqual(second.json()["detail"], "Finding not found")

    # export ----------------------------------------------------------------

    def test_export_markdown_contains_report_and_evidence(self) -> None:
        response = self._post_findings(
            [
                _finding(
                    title="XSS in search",
                    severity="critical",
                    evidence="proof-payload-<script>alert(1)</script>",
                    impact="session theft",
                    remediation="encode output",
                ),
                _finding(
                    title="Missing security headers",
                    severity="info",
                    vuln_type="info_leak",
                    target="static.example.com",
                    endpoint="/",
                ),
            ]
        )
        self.assertEqual(response.status_code, 201, response.text)

        export = self.client.get(
            f"/projects/{self.project_id}/findings/export", headers=self._bearer()
        )
        self.assertEqual(export.status_code, 200, export.text)
        self.assertTrue(
            export.headers["content-type"].startswith("text/markdown"),
            export.headers["content-type"],
        )
        self.assertEqual(
            export.headers["content-disposition"],
            f'attachment; filename="{self.project_id}-findings.md"',
        )
        text = export.text
        self.assertIn("# 漏洞报告：Test project", text)
        self.assertIn(f"- 项目：{self.project_id}（Test project）", text)
        self.assertIn("## 概览", text)
        self.assertIn("## 严重度汇总", text)
        self.assertIn("- 漏洞总数：2", text)
        self.assertIn("### Critical", text)
        self.assertIn("### Info", text)
        self.assertIn("#### v001 XSS in search", text)
        self.assertIn("proof-payload-<script>alert(1)</script>", text)
        self.assertIn("session theft", text)
        self.assertIn("encode output", text)
        self.assertNotIn("### High", text)

    def test_export_empty_project_still_renders(self) -> None:
        response = self.client.get(
            f"/projects/{self.project_id}/findings/export", headers=self._bearer()
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("- 漏洞总数：0", response.text)
        self.assertIn("暂无漏洞记录。", response.text)

    # capacity --------------------------------------------------------------

    def test_capacity_limit_returns_400_for_new_findings(self) -> None:
        from cairn.server.models import MAX_FINDINGS_PER_PROJECT, finding_fingerprint

        self.assertEqual(MAX_FINDINGS_PER_PROJECT, 500)
        now = "2020-01-01T00:00:00Z"
        rows = []
        for index in range(MAX_FINDINGS_PER_PROJECT):
            title = f"Seed {index}"
            rows.append(
                (
                    f"seed{index:03d}",
                    self.project_id,
                    title,
                    "info",
                    "open",
                    finding_fingerprint(title, "", "", ""),
                    now,
                    now,
                )
            )
        with self.db.get_conn() as conn:
            conn.executemany(
                "INSERT INTO findings "
                "(id, project_id, title, severity, status, fingerprint, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

        rejected = self._post_findings([_finding(title="One too many")])
        self.assertEqual(rejected.status_code, 400, rejected.text)
        self.assertEqual(
            rejected.json()["detail"],
            "too many findings for project (max 500)",
        )

        # Re-reporting an existing fingerprint at capacity is an update, not an insert.
        updated = self._post_findings(
            [_finding(title="Seed 0", target="", endpoint="", vuln_type="")]
        )
        self.assertEqual(updated.status_code, 201, updated.text)
        self.assertEqual(updated.json()["created"], 0)
        self.assertEqual(updated.json()["updated"], 1)
        self.assertEqual(self._get_findings().json()["summary"]["total"], 500)

    # auth ------------------------------------------------------------------

    def test_unauthenticated_requests_are_rejected(self) -> None:
        self.assertEqual(self.client.get(f"/projects/{self.project_id}/findings").status_code, 401)
        self.assertEqual(
            self.client.post(
                f"/projects/{self.project_id}/findings", json={"findings": [_finding()]}
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                f"/projects/{self.project_id}/findings/export"
            ).status_code,
            401,
        )

    # project list finding_count -------------------------------------------

    def test_project_list_reports_finding_count(self) -> None:
        other_project = self._create_project("Other project")
        self.assertEqual(
            len(self._post_findings([_finding(), _finding(title="Second")]).json()["findings"]),
            2,
        )
        self.assertEqual(
            self._post_findings(
                [_finding(title="Only one")], project_id=other_project
            ).json()["created"],
            1,
        )

        response = self.client.get("/projects", headers=self._bearer())
        self.assertEqual(response.status_code, 200, response.text)
        counts = {item["id"]: item["finding_count"] for item in response.json()}
        self.assertEqual(counts[self.project_id], 2)
        self.assertEqual(counts[other_project], 1)


if __name__ == "__main__":
    unittest.main()
