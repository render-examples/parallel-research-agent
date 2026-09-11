"""Regression coverage for the completed Render task response envelope.

Run with: python -m unittest discover -s tests
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from render.public_api.models.task_run_details import TaskRunDetails

from gateway import main as gateway


class ResearchResultsTest(unittest.TestCase):
    def get_result(self, results, status="succeeded"):
        run = TaskRunDetails.from_dict({
            "id": "test-run",
            "taskId": "test-task",
            "status": status,
            "results": results,
            "input": {"query": "Test research question"},
            "parentTaskRunId": "",
            "rootTaskRunId": "test-run",
            "retries": 0,
            "attempts": [],
        })
        client = SimpleNamespace(workflows=SimpleNamespace(
            get_task_run=lambda run_id: run,
        ))
        with patch.object(gateway, "_client", return_value=client):
            with TestClient(gateway.app) as http:
                response = http.get("/research/test-run")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_completed_task_returns_report_from_sdk_results_list(self):
        report = {
            "report": "A cited finding [source](https://example.org).",
            "sources": ["https://example.org"],
            "branches_completed": 5,
            "branches_failed": 0,
        }
        self.assertEqual(self.get_result([report]), {
            "run_id": "test-run", "status": "completed", "result": report,
        })

    def test_legacy_dictionary_result_is_preserved(self):
        report = {"report": "Completed research"}
        self.assertEqual(self.get_result(report)["result"], report)

    def test_missing_or_ambiguous_results_are_not_reports(self):
        for results in (None, [], [None], ["text"], [{}, {}]):
            with self.subTest(results=results):
                self.assertIsNone(self.get_result(results)["result"])

    def test_unfinished_task_does_not_publish_result(self):
        self.assertIsNone(self.get_result([{"report": "partial"}], "running")["result"])
