import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backlogtalk import core


class ParseTests(unittest.TestCase):
    def test_multiple(self):
        x = core.parse_tickets(
            '{"tickets":[{"title":"Mejorar logs de sync","description":"Agregar eventos","type":"Task"}, {"title":"Corregir los reintentos","description":"Resolver fallo","type":"Bug"}]}'
        )
        self.assertEqual(len(x), 2)
        self.assertEqual(x[1]["type"], "Bug")

    def test_empty(self):
        self.assertEqual(core.parse_tickets('{"tickets":[]}'), [])

    def test_invalid(self):
        with self.assertRaises(ValueError):
            core.parse_tickets("un texto")

    def test_prompt_has_skill(self):
        from unittest.mock import patch

        with patch.object(core, "load_skill", return_value="Criterios de aceptación"):
            self.assertIn("Criterios de aceptación", core.build_draft_prompt("revisar APIs"))

    def test_draft_includes_only_matching_project_and_epic_context(self):
        from unittest.mock import patch

        config = {
            "jira_contexts": {
                "example.com": {
                    "DEMO": {
                        "instructions": "regla proyecto",
                        "epics": {"DEMO-1": "regla épica uno", "DEMO-2": "regla épica dos"},
                    },
                    "OTRO": {"instructions": "otro proyecto"},
                }
            }
        }
        with patch.object(core, "load_skill", return_value="regla general"):
            prompt = core.build_draft_prompt("nota", "example.com", "DEMO", "DEMO-1", config)
            other = core.build_draft_prompt("nota", "different.com", "DEMO", "DEMO-1", config)
        for text in ("regla general", "regla proyecto", "regla épica uno"):
            self.assertIn(text, prompt)
        for text in ("regla épica dos", "otro proyecto"):
            self.assertNotIn(text, prompt)
        self.assertNotIn("regla proyecto", other)

    def test_agent_plan_receives_all_matching_instructions_and_metadata(self):
        from subprocess import CompletedProcess
        from unittest.mock import patch

        ticket = {
            "project_key": "DEMO",
            "epic_key": "DEMO-1",
            "title": "Una tarea",
            "description": "Descripción",
            "ticket_type": "Task",
        }
        fields = [{"id": "customfield_10010", "name": "Equipo", "required": True}]
        with (
            patch.object(core, "load_skill", return_value="Regla global"),
            patch.object(core, "find_cursor", return_value="agent"),
            patch.object(
                core.subprocess, "run", return_value=CompletedProcess([], 0, '{"fields":{}}', "")
            ) as run,
        ):
            core.plan_jira_fields(
                ticket,
                "example.com",
                fields,
                "Regla del proyecto",
                "Regla de la épica",
                {"model_backend": "cursor"},
            )
        prompt = run.call_args.kwargs["input"]
        for text in (
            "Regla global",
            "Regla del proyecto",
            "Regla de la épica",
            "customfield_10010",
            "DEMO-1",
        ):
            self.assertIn(text, prompt)


class DatabaseMigrationTests(unittest.TestCase):
    def test_existing_tickets_keep_their_data_when_site_is_added(self):
        import sqlite3
        import tempfile
        from contextlib import closing
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "old.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.execute(
                    "CREATE TABLE tickets (id INTEGER PRIMARY KEY, title TEXT, jira_key TEXT)"
                )
                db.execute("INSERT INTO tickets VALUES (1, 'Guardado', 'DEMO-1')")
                db.commit()
            with patch.multiple(core, APP_HOME=root, AUDIO_DIR=root / "audio", DB_FILE=path):
                with core.db_connect() as db:
                    ticket = db.execute("SELECT * FROM tickets WHERE id=1").fetchone()
                    self.assertEqual((ticket["title"], ticket["jira_key"]), ("Guardado", "DEMO-1"))
                    self.assertIsNone(ticket["jira_site"])


class PublicationTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest.mock import patch

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(core, "APP_HOME", root),
            patch.object(core, "AUDIO_DIR", root / "audio"),
            patch.object(core, "DB_FILE", root / "test.sqlite3"),
            patch.object(core, "CONFIG_DIR", root / "config"),
            patch.object(core, "CONFIG_FILE", root / "config/config.json"),
            patch.object(
                core,
                "plan_jira_fields",
                return_value='{"fields":{},"explanation":"Sin campos adicionales"}',
            ),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.tmp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)
        capture = core.add_capture("DEMO", "DEMO-414", "nota")
        core.save_drafts(
            capture, [{"title": "Prueba de tarea", "description": "Descripción", "type": "Task"}]
        )
        self.ticket = core.list_tickets()[0]["id"]

    def test_daily_activity_counts_local_events_and_only_reported_ollama_tokens(self):
        from datetime import UTC, datetime, timedelta

        self.assertEqual(core.daily_activity(), (0, None))
        core.record_ollama_usage({"prompt_eval_count": 12, "eval_count": 8})
        core.record_ollama_usage({"prompt_eval_count": 5})
        core.record_ollama_usage({"prompt_eval_count": True, "eval_count": 1})
        with core.db_connect() as db:
            db.execute("INSERT INTO ticket_publications(verified_at) VALUES (?)", (core.utc_now(),))
            db.execute(
                "INSERT INTO ticket_publications(verified_at) VALUES (?)",
                ((datetime.now(UTC) - timedelta(days=3)).isoformat(),),
            )
        self.assertEqual(core.daily_activity(), (1, 20))
        core.delete_ticket(self.ticket)
        self.assertEqual(core.daily_activity(), (1, 20))

    def test_ollama_draft_records_returned_usage(self):
        import json
        from unittest.mock import MagicMock, patch

        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"message": {"content": '{"tickets":[]}'}, "prompt_eval_count": 7, "eval_count": 4}
        ).encode()
        with patch.object(core.urllib.request, "urlopen", return_value=response):
            self.assertEqual(core.draft_with_ollama("Nota", "modelo"), [])
        self.assertEqual(core.daily_activity(), (0, 11))

    def test_field_cache_is_scoped_and_preserved_after_partial_sync(self):
        from unittest.mock import patch

        field = {"id": "customfield_101", "name": "Puntos", "schema": {"type": "number"}}
        with (
            patch.object(core, "jira_types", return_value=["Tarea", "Error"]),
            patch.object(
                core, "jira_priority_options", return_value=[{"label": "Media", "value": "3"}]
            ),
            patch.object(
                core, "jira_create_fields", side_effect=[[field], RuntimeError("sin acceso")]
            ),
        ):
            errors = core.sync_jira_fields("example.atlassian.net", ["DEMO"])
        self.assertIn("DEMO / Error", errors)
        self.assertEqual(
            core.cached_jira_fields("example.atlassian.net", "DEMO", "Task")[0], [field]
        )
        self.assertIsNone(core.cached_jira_fields("other.atlassian.net", "DEMO", "Task"))
        with (
            patch.object(core, "jira_types", return_value=["Tarea"]),
            patch.object(core, "jira_priority_options", side_effect=RuntimeError("sin conexión")),
            patch.object(core, "jira_create_fields", side_effect=RuntimeError("sin conexión")),
        ):
            core.sync_jira_fields("example.atlassian.net", ["DEMO"])
        self.assertEqual(
            core.cached_jira_fields("example.atlassian.net", "DEMO", "Task")[0], [field]
        )
        self.assertEqual(
            core.cached_jira_priorities("example.atlassian.net"), [{"label": "Media", "value": "3"}]
        )

    def test_sync_resolves_team_and_sprint_options(self):
        from unittest.mock import patch

        fields = [
            {"id": "customfield_1", "schema": {"type": "team"}},
            {"id": "customfield_2", "schema": {"custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
        ]
        team = [{"label": "Equipo", "value": "0454b33c-a128-4720-b0ee-47ad7b7e8983"}]
        sprint = [{"label": "Sprint 8 (activo)", "value": 123}]
        with (
            patch.object(core, "jira_types", return_value=["Tarea"]),
            patch.object(
                core, "jira_priority_options", return_value=[{"label": "Media", "value": "3"}]
            ),
            patch.object(core, "jira_create_fields", return_value=fields),
            patch.object(core, "jira_team_options", return_value=team),
            patch.object(core, "jira_sprint_options", return_value=sprint),
        ):
            self.assertEqual(core.sync_jira_fields("example.atlassian.net", ["DEMO"]), {})
        cached, _ = core.cached_jira_fields("example.atlassian.net", "DEMO", "Task")
        self.assertEqual(cached[0]["backlogtalk_options"], team)
        self.assertEqual(cached[1]["backlogtalk_options"], sprint)
        fresh_fields = [
            {key: value for key, value in field.items() if key != "backlogtalk_options"}
            for field in fields
        ]
        with (
            patch.object(core, "jira_types", return_value=["Tarea"]),
            patch.object(
                core, "jira_priority_options", return_value=[{"label": "Media", "value": "3"}]
            ),
            patch.object(core, "jira_create_fields", return_value=fresh_fields),
            patch.object(core, "jira_team_options", side_effect=RuntimeError("sin acceso")),
            patch.object(core, "jira_sprint_options", return_value=sprint),
        ):
            errors = core.sync_jira_fields("example.atlassian.net", ["DEMO"])
        self.assertIn("DEMO / equipos", errors)
        cached, _ = core.cached_jira_fields("example.atlassian.net", "DEMO", "Task")
        self.assertEqual(cached[0]["backlogtalk_options"], team)

    def test_special_field_options_use_native_ids_and_paginate_boards(self):
        from unittest.mock import patch

        team_id = "0454b33c-a128-4720-b0ee-47ad7b7e8983"
        with patch.object(
            core,
            "twg_json",
            return_value=[
                {"team": {"id": "ari:cloud:identity::team/" + team_id, "displayName": "Equipo"}}
            ],
        ):
            self.assertEqual(
                core.jira_team_options("example.atlassian.net"),
                [{"label": "Equipo", "value": team_id}],
            )
        pages = [
            {"boards": [{"id": 10}], "isLast": False},
            {"boards": [{"id": 11}], "isLast": True},
            {"sprints": [{"id": 101, "name": "Sprint A"}], "isLast": True},
            {"sprints": [], "isLast": True},
            {"sprints": [{"id": 101, "name": "Sprint A"}], "isLast": True},
            {"sprints": [{"id": 102, "name": "Sprint B"}], "isLast": True},
        ]
        with patch.object(core, "twg_json", side_effect=pages):
            options = core.jira_sprint_options("example.atlassian.net", "DEMO")
        self.assertEqual({option["value"] for option in options}, {101, 102})

    def test_publish_checks_team_and_sprint_and_preserves_wire_values(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import patch

        team_id = "0454b33c-a128-4720-b0ee-47ad7b7e8983"
        fields = [
            {"id": "customfield_1", "name": "Team", "schema": {"type": "team"}},
            {
                "id": "customfield_2",
                "name": "Sprint",
                "schema": {"custom": "com.pyxis.greenhopper.jira:gh-sprint"},
            },
        ]
        responses = [
            {"data": [{"name": "Tarea", "hierarchyLevel": 0}]},
            {"data": {"fields": fields}},
            {
                "data": [
                    {
                        "key": "DEMO-414",
                        "project": {"key": "DEMO"},
                        "issuetype": {"hierarchyLevel": 1},
                    }
                ]
            },
        ]
        core.save_ticket_extra_fields(
            self.ticket,
            "example.atlassian.net",
            "Task",
            {"customfield_1": team_id, "customfield_2": 123},
        )

        def approve(plan):
            self.assertEqual(plan["fields"], {"customfield_1": team_id, "customfield_2": 123})
            return False

        with (
            patch.object(core, "find_twg", return_value="twg"),
            patch.object(
                core, "jira_team_options", return_value=[{"label": "Equipo", "value": team_id}]
            ),
            patch.object(
                core, "jira_sprint_options", return_value=[{"label": "Sprint", "value": 123}]
            ),
            patch.object(
                core.subprocess,
                "run",
                side_effect=[CompletedProcess([], 0, json.dumps(value), "") for value in responses],
            ) as run,
            self.assertRaises(core.PublicationCancelled),
        ):
            core.publish_ticket(self.ticket, "example.atlassian.net", approve_plan=approve)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(core.get_ticket(self.ticket)["state"], "draft")

    def test_saved_fields_are_scoped_and_deleted_with_ticket(self):
        core.save_ticket_extra_fields(
            self.ticket, "example.atlassian.net", "Task", {"customfield_101": 3}
        )
        self.assertEqual(
            core.ticket_extra_fields(self.ticket, "example.atlassian.net", "Task"),
            {"customfield_101": 3},
        )
        self.assertEqual(core.ticket_extra_fields(self.ticket, "example.atlassian.net", "Bug"), {})
        core.delete_ticket(self.ticket)
        with core.db_connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM ticket_field_values").fetchone()[0], 0
            )

    def test_removed_priority_is_rejected_before_jira_creation(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import patch

        core.save_ticket_extra_fields(
            self.ticket, "example.atlassian.net", "Task", {"standard:priority": "1"}
        )
        with core.db_connect() as db:
            db.execute(
                "INSERT INTO jira_priority_cache VALUES (?,?,?)",
                ("example.atlassian.net", '[{"label":"Highest","value":"1"}]', "ahora"),
            )
        responses = [
            CompletedProcess(
                [], 0, json.dumps({"data": [{"name": "Tarea", "hierarchyLevel": 0}]}), ""
            ),
            CompletedProcess([], 0, json.dumps({"data": {"fields": []}}), ""),
        ]
        with (
            patch.object(core, "find_twg", return_value="twg"),
            patch.object(
                core, "jira_priority_options", return_value=[{"label": "Medium", "value": "3"}]
            ),
            patch.object(core.subprocess, "run", side_effect=responses) as run,
            self.assertRaisesRegex(ValueError, "prioridad ya no está disponible"),
        ):
            core.publish_ticket(self.ticket, "example.atlassian.net")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(core.get_ticket(self.ticket)["state"], "draft")

    def test_reviewed_fields_override_agent_and_are_checked_against_current_options(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import patch

        field = {
            "id": "customfield_101",
            "name": "Equipo",
            "required": True,
            "allowedValues": [{"id": "2", "value": "Equipo A"}, {"id": "3", "value": "Equipo B"}],
        }
        types = {"data": [{"name": "Tarea", "hierarchyLevel": 0}]}
        metadata = {"data": {"fields": [field]}}
        epic = {
            "data": [
                {"key": "DEMO-414", "project": {"key": "DEMO"}, "issuetype": {"hierarchyLevel": 1}}
            ]
        }

        def response(data):
            return CompletedProcess([], 0, json.dumps(data), "")

        core.save_ticket_extra_fields(
            self.ticket, "example.atlassian.net", "Task", {"customfield_101": {"id": "3"}}
        )
        with (
            patch.object(core, "find_twg", return_value="twg"),
            patch.object(
                core, "plan_jira_fields", return_value='{"fields":{"customfield_101":{"id":"2"}}}'
            ),
            patch.object(
                core.subprocess,
                "run",
                side_effect=[response(types), response(metadata), response(epic)],
            ) as run,
            self.assertRaises(core.PublicationCancelled),
        ):
            core.publish_ticket(
                self.ticket,
                "example.atlassian.net",
                approve_plan=lambda plan: (
                    self.assertEqual(plan["fields"]["customfield_101"], {"id": "3"}) or False
                ),
            )
        self.assertEqual(run.call_count, 3)
        core.save_ticket_extra_fields(
            self.ticket, "example.atlassian.net", "Task", {"customfield_101": {"id": "retirado"}}
        )
        with (
            patch.object(core, "find_twg", return_value="twg"),
            patch.object(
                core.subprocess, "run", side_effect=[response(types), response(metadata)]
            ) as run,
            self.assertRaisesRegex(ValueError, "ya no está disponible"),
        ):
            core.publish_ticket(self.ticket, "example.atlassian.net")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(core.get_ticket(self.ticket)["state"], "draft")

    def test_publishes_once_and_verifies_parent(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import patch

        epic = {
            "data": [
                {"key": "DEMO-414", "project": {"key": "DEMO"}, "issuetype": {"hierarchyLevel": 1}}
            ]
        }
        created = {"data": {"key": "DEMO-999"}}
        verify = {
            "data": [
                {
                    "key": "DEMO-999",
                    "summary": "Prueba de tarea",
                    "project": {"key": "DEMO"},
                    "parent": {"key": "DEMO-414"},
                }
            ]
        }
        types = {"data": [{"name": "Tarea", "hierarchyLevel": 0}]}
        metadata = {"data": {"fields": []}}
        responses = [
            CompletedProcess([], 0, json.dumps(x), "")
            for x in (types, metadata, epic, created, verify)
        ]
        with (
            patch.object(core.shutil, "which", return_value="/bin/twg"),
            patch.object(core.subprocess, "run", side_effect=responses) as run,
        ):
            self.assertEqual(
                core.publish_ticket(self.ticket, "example.atlassian.net")[0], "DEMO-999"
            )
        self.assertEqual(core.get_ticket(self.ticket)["state"], "published")
        self.assertEqual(core.get_ticket(self.ticket)["jira_site"], "example.atlassian.net")
        self.assertEqual(core.daily_activity()[0], 1)
        self.assertIn("Sin campos adicionales", core.get_ticket(self.ticket)["agent_response"])
        self.assertIn("Creación", core.get_ticket(self.ticket)["twg_output"])
        self.assertIn("Verificación", core.get_ticket(self.ticket)["twg_output"])
        self.assertIn("--parent", run.call_args_list[3].args[0])
        for call in run.call_args_list:
            self.assertIn("example.atlassian.net", call.args[0])

    def test_ambiguous_creation_blocks_retry(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import patch

        epic = {
            "data": [
                {"key": "DEMO-414", "project": {"key": "DEMO"}, "issuetype": {"hierarchyLevel": 1}}
            ]
        }
        types = {"data": [{"name": "Tarea", "hierarchyLevel": 0}]}
        metadata = {"data": {"fields": []}}
        responses = [CompletedProcess([], 0, json.dumps(x), "") for x in (types, metadata, epic)]
        responses.append(CompletedProcess([], 1, "", "timeout"))
        with (
            patch.object(core.shutil, "which", return_value="/bin/twg"),
            patch.object(core.subprocess, "run", side_effect=responses),
        ):
            with self.assertRaises(RuntimeError):
                core.publish_ticket(self.ticket, "example.atlassian.net")
        self.assertEqual(core.get_ticket(self.ticket)["state"], "uncertain")
        self.assertIn("timeout", core.get_ticket(self.ticket)["twg_output"])
        with self.assertRaises(ValueError):
            core.publish_ticket(self.ticket, "example.atlassian.net")

    def test_created_key_handles_nested_response_without_using_parent(self):
        self.assertEqual(
            core.created_issue_key(
                {"data": {"workItem": {"key": "DEMO-999", "parent": {"key": "DEMO-414"}}}}, "DEMO"
            ),
            "DEMO-999",
        )
        with self.assertRaises(RuntimeError):
            core.created_issue_key({"data": {"parent": {"key": "DEMO-414"}}}, "DEMO")
        with self.assertRaises(RuntimeError):
            core.created_issue_key({"data": [{"key": "DEMO-999"}, {"key": "DEMO-998"}]}, "DEMO")

    def test_comment_is_sent_only_after_verified_creation(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import patch

        epic = {
            "data": [
                {"key": "DEMO-414", "project": {"key": "DEMO"}, "issuetype": {"hierarchyLevel": 1}}
            ]
        }
        payloads = [
            {"data": [{"name": "Tarea", "hierarchyLevel": 0}]},
            {"data": {"fields": []}},
            epic,
            {"data": {"workItem": {"key": "DEMO-999"}}},
            {
                "data": [
                    {
                        "key": "DEMO-999",
                        "summary": "Prueba de tarea",
                        "project": {"key": "DEMO"},
                        "parent": {"key": "DEMO-414"},
                    }
                ]
            },
            {"data": {"id": "123"}},
        ]
        response = '{"fields":{},"standard":{"assignee":"me"},"comment":"Creado con BacklogTalk"}'
        with (
            patch.object(core.shutil, "which", return_value="/bin/twg"),
            patch.object(core, "plan_jira_fields", return_value=response),
            patch.object(
                core.subprocess,
                "run",
                side_effect=[CompletedProcess([], 0, json.dumps(x), "") for x in payloads],
            ) as run,
        ):
            self.assertEqual(
                core.publish_ticket(
                    self.ticket, "example.atlassian.net", approve_plan=lambda plan: True
                ),
                ("DEMO-999", ""),
            )
        self.assertIn("--assignee", run.call_args_list[3].args[0])
        self.assertIn("comment", run.call_args_list[5].args[0])
        self.assertIn("Comentario", core.get_ticket(self.ticket)["twg_output"])
        self.assertEqual(core.get_ticket(self.ticket)["state"], "published")

    def test_comment_failure_does_not_make_created_ticket_retryable(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import patch

        payloads = [
            {"data": [{"name": "Tarea", "hierarchyLevel": 0}]},
            {"data": {"fields": []}},
            {
                "data": [
                    {
                        "key": "DEMO-414",
                        "project": {"key": "DEMO"},
                        "issuetype": {"hierarchyLevel": 1},
                    }
                ]
            },
            {"data": {"key": "DEMO-999"}},
            {
                "data": [
                    {
                        "key": "DEMO-999",
                        "summary": "Prueba de tarea",
                        "project": {"key": "DEMO"},
                        "parent": {"key": "DEMO-414"},
                    }
                ]
            },
        ]
        responses = [CompletedProcess([], 0, json.dumps(x), "") for x in payloads] + [
            CompletedProcess([], 1, "", "falló comentario")
        ]
        with (
            patch.object(core.shutil, "which", return_value="/bin/twg"),
            patch.object(
                core, "plan_jira_fields", return_value='{"fields":{},"comment":"Comentario"}'
            ),
            patch.object(core.subprocess, "run", side_effect=responses),
        ):
            key, warning = core.publish_ticket(
                self.ticket, "example.atlassian.net", approve_plan=lambda plan: True
            )
        self.assertEqual(key, "DEMO-999")
        self.assertIn("comentario", warning)
        self.assertEqual(core.get_ticket(self.ticket)["state"], "published")
        self.assertIn("falló comentario", core.get_ticket(self.ticket)["twg_output"])
        with self.assertRaises(ValueError):
            core.publish_ticket(self.ticket, "example.atlassian.net")

    def test_agent_fields_and_comment_require_explicit_approval(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import patch

        data = [
            {"data": [{"name": "Tarea", "hierarchyLevel": 0}]},
            {"data": {"fields": []}},
            {
                "data": [
                    {
                        "key": "DEMO-414",
                        "project": {"key": "DEMO"},
                        "issuetype": {"hierarchyLevel": 1},
                    }
                ]
            },
        ]
        response = '{"fields":{},"standard":{"assignee":"alguien"},"comment":"Texto propuesto"}'
        for approval in (None, lambda plan: False):
            with (
                self.subTest(approval=approval),
                patch.object(core.shutil, "which", return_value="/bin/twg"),
                patch.object(core, "plan_jira_fields", return_value=response),
                patch.object(
                    core.subprocess,
                    "run",
                    side_effect=[CompletedProcess([], 0, json.dumps(x), "") for x in data],
                ) as run,
            ):
                with self.assertRaises(ValueError):
                    core.publish_ticket(self.ticket, "example.atlassian.net", approve_plan=approval)
            self.assertEqual(run.call_count, 3)
            self.assertEqual(core.get_ticket(self.ticket)["state"], "draft")

    def test_changed_ticket_is_not_created_after_plan_approval(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import patch

        payloads = [
            {"data": [{"name": "Tarea", "hierarchyLevel": 0}]},
            {"data": {"fields": []}},
            {
                "data": [
                    {
                        "key": "DEMO-414",
                        "project": {"key": "DEMO"},
                        "issuetype": {"hierarchyLevel": 1},
                    }
                ]
            },
        ]

        def approve(_plan):
            core.edit_ticket(self.ticket, "Título cambiado", "Descripción", "Task", "DEMO-414")
            return True

        with (
            patch.object(core.shutil, "which", return_value="/bin/twg"),
            patch.object(
                core.subprocess,
                "run",
                side_effect=[CompletedProcess([], 0, json.dumps(x), "") for x in payloads],
            ) as run,
        ):
            with self.assertRaises(ValueError):
                core.publish_ticket(self.ticket, "example.atlassian.net", approve_plan=approve)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(core.get_ticket(self.ticket)["state"], "draft")

    def test_required_fields_are_supplied_once_and_scoped_context_reaches_agent(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import patch

        config = core.load_config()
        config["jira_contexts"] = {
            "example.atlassian.net": {
                "DEMO": {"instructions": "Proyecto", "epics": {"DEMO-414": "Épica"}}
            }
        }
        core.save_config(config)
        fields = [{"id": "customfield_12345", "name": "Equipo", "required": True}]
        responses = [
            {"data": [{"name": "Tarea", "hierarchyLevel": 0}]},
            {"data": {"fields": fields}},
            {
                "data": [
                    {
                        "key": "DEMO-414",
                        "project": {"key": "DEMO"},
                        "issuetype": {"hierarchyLevel": 1},
                    }
                ]
            },
            {"data": {"key": "DEMO-999"}},
            {
                "data": [
                    {
                        "key": "DEMO-999",
                        "summary": "Prueba de tarea",
                        "project": {"key": "DEMO"},
                        "parent": {"key": "DEMO-414"},
                    }
                ]
            },
        ]
        response = '{"fields":{"customfield_12345":"equipo indicado"},"explanation":"Preparado"}'
        with (
            patch.object(core.shutil, "which", return_value="/bin/twg"),
            patch.object(core, "plan_jira_fields", return_value=response) as agent,
            patch.object(
                core.subprocess,
                "run",
                side_effect=[CompletedProcess([], 0, json.dumps(x), "") for x in responses],
            ) as run,
        ):
            core.publish_ticket(
                self.ticket, "example.atlassian.net", approve_plan=lambda plan: True
            )
        self.assertEqual(agent.call_args.args[3:5], ("Proyecto", "Épica"))
        command = run.call_args_list[3].args[0]
        self.assertEqual(
            json.loads(command[command.index("--fields-json") + 1]),
            {"customfield_12345": "equipo indicado"},
        )
        self.assertEqual(core.get_ticket(self.ticket)["agent_response"], response)

    def test_missing_or_unrecognized_field_prevents_creation_and_saves_response(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import patch

        types = {"data": [{"name": "Tarea", "hierarchyLevel": 0}]}
        metadata = {
            "data": {"fields": [{"id": "customfield_12345", "name": "Equipo", "required": True}]}
        }
        for response in (
            '{"fields":{},"explanation":"Falta equipo"}',
            '{"fields":{"customfield_99999":"x"}}',
        ):
            with (
                self.subTest(response=response),
                patch.object(core.shutil, "which", return_value="/bin/twg"),
                patch.object(core, "plan_jira_fields", return_value=response),
                patch.object(
                    core.subprocess,
                    "run",
                    side_effect=[
                        CompletedProcess([], 0, json.dumps(x), "") for x in (types, metadata)
                    ],
                ) as run,
            ):
                with self.assertRaises(ValueError):
                    core.publish_ticket(self.ticket, "example.atlassian.net")
            self.assertEqual(run.call_count, 2)
            self.assertEqual(core.get_ticket(self.ticket)["state"], "draft")
            self.assertEqual(core.get_ticket(self.ticket)["agent_response"], response)

    def test_required_standard_fields_are_checked_against_the_proposed_ticket(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import patch

        fields = [
            {"id": name, "name": name, "required": True}
            for name in ("summary", "description", "project", "issuetype", "parent", "assignee")
        ]
        payloads = [
            {"data": [{"name": "Tarea", "hierarchyLevel": 0}]},
            {"data": {"fields": fields}},
            {
                "data": [
                    {
                        "key": "DEMO-414",
                        "project": {"key": "DEMO"},
                        "issuetype": {"hierarchyLevel": 1},
                    }
                ]
            },
            {"data": {"key": "DEMO-999"}},
            {
                "data": [
                    {
                        "key": "DEMO-999",
                        "summary": "Prueba de tarea",
                        "project": {"key": "DEMO"},
                        "parent": {"key": "DEMO-414"},
                    }
                ]
            },
        ]
        response = '{"fields":{},"standard":{"assignee":"me"}}'
        with (
            patch.object(core.shutil, "which", return_value="/bin/twg"),
            patch.object(core, "plan_jira_fields", return_value=response),
            patch.object(
                core.subprocess,
                "run",
                side_effect=[CompletedProcess([], 0, json.dumps(x), "") for x in payloads],
            ),
        ):
            self.assertEqual(
                core.publish_ticket(
                    self.ticket, "example.atlassian.net", approve_plan=lambda plan: True
                )[0],
                "DEMO-999",
            )

    def test_missing_required_standard_field_prevents_creation(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import patch

        payloads = [
            {"data": [{"name": "Tarea", "hierarchyLevel": 0}]},
            {"data": {"fields": [{"id": "assignee", "name": "Responsable", "required": True}]}},
        ]
        with (
            patch.object(core.shutil, "which", return_value="/bin/twg"),
            patch.object(
                core.subprocess,
                "run",
                side_effect=[CompletedProcess([], 0, json.dumps(x), "") for x in payloads],
            ) as run,
        ):
            with self.assertRaisesRegex(ValueError, "Responsable"):
                core.publish_ticket(self.ticket, "example.atlassian.net")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(core.get_ticket(self.ticket)["state"], "draft")

    def test_changing_capture_destination_is_saved(self):
        capture = core.add_capture("DEMO", "DEMO-414", "a")
        core.update_capture(capture, "b", "DEMO", "DEMO-2252")
        with core.db_connect() as db:
            self.assertEqual(
                db.execute("SELECT epic_key FROM captures WHERE id=?", (capture,)).fetchone()[0],
                "DEMO-2252",
            )


class TranscriptionModelTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch

        decoder = patch.object(core, "decode_audio", return_value=object())
        decoder.start()
        self.addCleanup(decoder.stop)

    def test_model_is_reused_for_successive_notes(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        model = SimpleNamespace(
            transcribe=lambda *args, **kwargs: ([SimpleNamespace(text=" Hola ")], None)
        )
        core._whisper_model.cache_clear()
        try:
            with patch("faster_whisper.WhisperModel", return_value=model) as constructor:
                self.assertEqual(core.transcribe(Path("first.wav"), "small"), "Hola")
                self.assertEqual(core.transcribe(Path("second.wav"), "small"), "Hola")
                self.assertEqual(constructor.call_count, 1)
        finally:
            core._whisper_model.cache_clear()

    def test_transcription_sends_predecoded_audio_to_model(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        decoded = object()
        model = SimpleNamespace(
            transcribe=MagicMock(return_value=(iter([]), SimpleNamespace(duration=1)))
        )
        with (
            patch.object(core, "decode_audio", return_value=decoded),
            patch.object(core, "_whisper_model", return_value=model),
        ):
            self.assertEqual(core.transcribe(Path("audio.wav"), "small"), "")
        self.assertIs(model.transcribe.call_args.args[0], decoded)

    def test_transcription_uses_selected_language_without_detection(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        model = SimpleNamespace(
            transcribe=MagicMock(return_value=(iter([]), SimpleNamespace(duration=1)))
        )
        with patch.object(core, "_whisper_model", return_value=model):
            self.assertEqual(core.transcribe(Path("audio.wav"), "small"), "")
            self.assertEqual(model.transcribe.call_args.kwargs["language"], "es")
            self.assertEqual(core.transcribe(Path("audio.wav"), "small", language="en"), "")
            self.assertEqual(model.transcribe.call_args.kwargs["language"], "en")
        with self.assertRaises(ValueError):
            core.transcribe(Path("audio.wav"), "small.en", language="es")
        with self.assertRaises(ValueError):
            core.transcribe(Path("audio.wav"), "small", language="fr")

    def test_progress_reports_real_segment_positions_and_duration(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        segments = [SimpleNamespace(text="Uno", end=10), SimpleNamespace(text="Dos", end=35)]
        model = SimpleNamespace(
            transcribe=lambda *args, **kwargs: (iter(segments), SimpleNamespace(duration=40))
        )
        events = []
        with patch.object(core, "_whisper_model", return_value=model):
            result = core.transcribe(
                Path("audio.wav"), "small", progress=lambda *event: events.append(event)
            )
        self.assertEqual(result, "Uno Dos")
        self.assertEqual([(p, t) for p, t, _ in events], [(0.0, 40.0), (10, 40.0), (35, 40.0)])
        self.assertTrue(all(elapsed >= 0 for _, _, elapsed in events))


class OperationRecoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest.mock import patch

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, value in [
            ("APP_HOME", root),
            ("AUDIO_DIR", root / "audio"),
            ("DB_FILE", root / "inbox.sqlite3"),
        ]:
            item = patch.object(core, name, value)
            item.start()
            self.addCleanup(item.stop)
        core.db_connect().close()

    def test_interrupted_operation_detection_and_persisted_error(self):
        capture = core.add_capture("DEMO", None, audio="audio.wav")
        operation = core.begin_operation("transcripción", capture, "Modelo: small")
        self.assertEqual(core.interrupted_operations(), [])
        with core.db_connect() as db:
            db.execute(
                "UPDATE processing_operations SET owner_pid=?, owner_started=NULL WHERE operation_id=?",
                (2147483647, operation),
            )
        interrupted = core.interrupted_operations()
        self.assertEqual(len(interrupted), 1)
        self.assertEqual(interrupted[0]["capture_id"], capture)
        core.save_capture_error(capture, "interrumpida", "mensaje simple", "detalles copiables")
        core.finish_operation(operation)
        self.assertEqual(core.interrupted_operations(), [])
        self.assertEqual(core.get_capture_error(capture)["details"], "detalles copiables")
        core.delete_capture(capture)
        self.assertIsNone(core.get_capture_error(capture))


class DeletionTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest.mock import patch

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, value in [
            ("APP_HOME", root),
            ("AUDIO_DIR", root / "audio"),
            ("DB_FILE", root / "db.sqlite3"),
        ]:
            p = patch.object(core, name, value)
            p.start()
            self.addCleanup(p.stop)
        core.db_connect().close()

    def test_delete_note_removes_managed_audio_and_preserves_linked_ticket(self):
        audio = core.AUDIO_DIR / "recording.wav"
        audio.write_bytes(b"private")
        capture = core.add_capture("DEMO", "DEMO-414", "texto privado", str(audio))
        core.save_drafts(
            capture,
            [{"title": "Una tarea de ejemplo", "description": "Descripción", "type": "Task"}],
        )
        self.assertFalse(core.delete_capture(capture))
        self.assertFalse(audio.exists())
        self.assertEqual(core.list_captures(), [])
        ticket = core.list_tickets()[0]
        self.assertEqual(ticket["title"], "Una tarea de ejemplo")
        with core.db_connect() as db:
            row = db.execute("SELECT * FROM captures WHERE id=?", (capture,)).fetchone()
            self.assertEqual(
                (
                    row["state"],
                    row["transcript"],
                    row["audio_file"],
                    row["project_key"],
                    row["epic_key"],
                ),
                ("deleted", "", None, "", None),
            )
        core.delete_ticket(ticket["id"])
        with core.db_connect() as db:
            self.assertIsNone(
                db.execute("SELECT id FROM captures WHERE id=?", (capture,)).fetchone()
            )

    @unittest.skipUnless(__import__("os").name == "posix", "Permisos POSIX")
    def test_managed_directories_and_database_are_private(self):
        import stat

        self.assertEqual(stat.S_IMODE(core.APP_HOME.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(core.AUDIO_DIR.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(core.DB_FILE.stat().st_mode), 0o600)

    def test_concurrent_connections_share_initialized_schema(self):
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=6) as pool:
            ids = list(pool.map(lambda i: core.add_capture("DEMO", None, f"Nota {i}"), range(12)))
        self.assertEqual(len(set(ids)), 12)

    def test_delete_note_retains_external_or_shared_audio(self):
        external = core.APP_HOME / "external.wav"
        external.write_bytes(b"external")
        capture = core.add_capture("DEMO", None, audio=str(external))
        self.assertTrue(core.delete_capture(capture))
        self.assertTrue(external.exists())
        shared = core.AUDIO_DIR / "shared.wav"
        shared.write_bytes(b"shared")
        first = core.add_capture("DEMO", None, audio=str(shared))
        second = core.add_capture("DEMO", None, audio=str(shared))
        self.assertTrue(core.delete_capture(first))
        self.assertTrue(shared.exists())
        self.assertFalse(core.delete_capture(second))
        self.assertFalse(shared.exists())

    def test_refuse_to_remove_uncertain_ticket_and_allow_published_record(self):
        capture = core.add_capture("DEMO", None, "Nota")
        core.save_drafts(
            capture,
            [{"title": "Una tarea de ejemplo", "description": "Descripción", "type": "Task"}],
        )
        ticket = core.list_tickets()[0]
        with core.db_connect() as db:
            db.execute("UPDATE tickets SET state='uncertain' WHERE id=?", (ticket["id"],))
        with self.assertRaises(ValueError):
            core.delete_ticket(ticket["id"])
        with core.db_connect() as db:
            db.execute(
                "UPDATE tickets SET state='published', jira_key='DEMO-123' WHERE id=?",
                (ticket["id"],),
            )
        core.delete_ticket(ticket["id"])
        self.assertEqual(core.list_tickets(), [])
        self.assertEqual(core.list_captures()[0]["transcript"], "Nota")

    def test_failed_audio_delete_keeps_history_record(self):
        from unittest.mock import patch

        audio = core.AUDIO_DIR / "recording.wav"
        audio.write_bytes(b"private")
        capture = core.add_capture("DEMO", None, "Nota", str(audio))
        with patch.object(Path, "unlink", side_effect=PermissionError("denegado")):
            with self.assertRaises(PermissionError):
                core.delete_capture(capture)
        self.assertEqual(core.list_captures()[0]["transcript"], "Nota")
        self.assertTrue(audio.exists())

    def test_database_failure_during_delete_restores_audio(self):
        import sqlite3

        audio = core.AUDIO_DIR / "recording.wav"
        audio.write_bytes(b"private")
        capture = core.add_capture("DEMO", None, "Nota", str(audio))
        with core.db_connect() as db:
            db.execute(
                "CREATE TRIGGER refuse_delete BEFORE DELETE ON captures BEGIN SELECT RAISE(ABORT, 'denegado'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            core.delete_capture(capture)
        self.assertEqual(audio.read_bytes(), b"private")
        self.assertEqual(core.list_captures()[0]["transcript"], "Nota")

    def test_update_missing_note_fails_instead_of_losing_edits(self):
        with self.assertRaises(ValueError):
            core.update_capture(99999, "Texto nuevo")

    def test_recover_interrupted_delete_restores_only_existing_note(self):
        audio = core.AUDIO_DIR / "recording.wav"
        audio.write_bytes(b"private")
        capture = core.add_capture("DEMO", None, "Nota", str(audio))
        backup = core.AUDIO_DIR / f".backlogtalk-delete-{capture}-{'a' * 32}.bak"
        audio.rename(backup)
        core.recover_deleted_audio()
        self.assertEqual(audio.read_bytes(), b"private")
        self.assertFalse(backup.exists())


class CustomSkillTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from unittest.mock import patch

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        p = patch.object(core, "CONFIG_DIR", Path(self.temp.name))
        p.start()
        self.addCleanup(p.stop)

    def test_custom_prompt_is_used_by_both_drafting_backends(self):
        import json
        from subprocess import CompletedProcess
        from unittest.mock import MagicMock, patch

        core.save_skill("Usar secciones Alcance y Entregable.")
        answer = '{"tickets":[]}'
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"message": {"content": answer}}
        ).encode()
        with patch.object(core.urllib.request, "urlopen", return_value=response) as urlopen:
            self.assertEqual(core.draft_with_ollama("Nota de ejemplo", "modelo"), [])
            request = urlopen.call_args.args[0]
            body = json.loads(request.data)
            self.assertIn("Usar secciones Alcance y Entregable.", body["messages"][0]["content"])
        with (
            patch.object(core, "find_cursor", return_value="agent"),
            patch.object(
                core.subprocess, "run", return_value=CompletedProcess([], 0, answer, "")
            ) as run,
        ):
            self.assertEqual(core.draft_with_cursor("Nota de ejemplo"), [])
            self.assertIn("Usar secciones Alcance y Entregable.", run.call_args.kwargs["input"])
            self.assertNotIn("Nota de ejemplo", run.call_args.args[0])

    def test_default_and_blank_rejection_preserve_previous_instructions(self):
        self.assertEqual(core.load_skill(), core.load_default_skill())
        core.save_skill("Una instrucción válida")
        with self.assertRaises(ValueError):
            core.save_skill("  \n")
        self.assertEqual(core.load_skill().strip(), "Una instrucción válida")


if __name__ == "__main__":
    unittest.main()
