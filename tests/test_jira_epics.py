import json
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from backlogtalk import core


class JiraEpicQueryTests(unittest.TestCase):
    def test_reads_all_pages_and_passes_cursor(self):
        pages = [
            {
                "data": {"issues": [{"key": "DEMO-1", "summary": "Uno"}]},
                "pageInfo": {"hasNextPage": True, "nextCursor": "next"},
            },
            {
                "data": {"issues": [{"key": "DEMO-2", "summary": "Dos"}]},
                "pageInfo": {"hasNextPage": False},
            },
        ]
        responses = [CompletedProcess([], 0, json.dumps(page), "") for page in pages]
        with (
            patch.object(core, "find_twg", return_value="twg"),
            patch.object(core.subprocess, "run", side_effect=responses) as run,
        ):
            self.assertEqual(
                core.jira_epics("DEMO", "example.atlassian.net"),
                [
                    {"key": "DEMO-1", "summary": "Uno"},
                    {"key": "DEMO-2", "summary": "Dos"},
                ],
            )
        self.assertIn("--after", run.call_args_list[1].args[0])
        self.assertEqual(run.call_args_list[1].args[0][-1], "next")

    def test_terminal_page_without_page_info(self):
        page = {"data": {"issues": [{"key": "DEMO-1", "summary": "Uno"}]}}
        response = CompletedProcess([], 0, json.dumps(page), "")
        with (
            patch.object(core, "find_twg", return_value="twg"),
            patch.object(core.subprocess, "run", return_value=response),
        ):
            self.assertEqual(
                core.jira_epics("DEMO", "example.atlassian.net"),
                [{"key": "DEMO-1", "summary": "Uno"}],
            )

    def test_invalid_pagination_does_not_return_partial_results(self):
        page = {"data": {"issues": []}, "pageInfo": {"hasNextPage": True, "nextCursor": "same"}}
        response = CompletedProcess([], 0, json.dumps(page), "")
        with (
            patch.object(core, "find_twg", return_value="twg"),
            patch.object(core.subprocess, "run", return_value=response),
        ):
            with self.assertRaisesRegex(RuntimeError, "incompleta"):
                core.jira_epics("DEMO", "example.atlassian.net")

    def test_rejects_jql_injection_before_running_cli(self):
        with patch.object(core, "find_twg") as find:
            with self.assertRaises(ValueError):
                core.jira_epics("DEMO OR project = OTHER", "example.atlassian.net")
        find.assert_not_called()
