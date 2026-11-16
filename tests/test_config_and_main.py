"""
Routing configuration and CLI/exit-status tests. Offline; the syncer is mocked.
"""
import dataclasses
import importlib
import logging
import os
import unittest
from unittest import mock

from src import config
from src.config import FILE_RULE_ALL, FILE_RULE_PDF_ONLY, Route

SRC = 1553121697288068
SRC_COL = 8022925527175044
MASTER = 4850043816202116
MASTER_COL = 1437881091182468
BLANK = 573385022328708
BLANK_COL = 7068369005875076
GOOD_KEY = "test-key-not-real-0123456789"

WR_TITLE = "Work Request #"
MASTER_ROUTE = Route("master-schedule", SRC, MASTER, SRC_COL, MASTER_COL, FILE_RULE_ALL, WR_TITLE, "WR")
BLANK_ROUTE = Route("blank-copy", SRC, BLANK, SRC_COL, BLANK_COL, FILE_RULE_PDF_ONLY, WR_TITLE, WR_TITLE)
ROUTING_VARS = ("SOURCE_SHEET_ID", "TARGET_SHEET_ID", "SOURCE_MATCH_COLUMN_ID", "TARGET_MATCH_COLUMN_ID")

# main.py calls load_dotenv() at import; never let tests read a real .env.
with mock.patch("dotenv.load_dotenv"):
    main = importlib.import_module("main")


class RoutingConfigTests(unittest.TestCase):
    def test_route_table_is_exactly_the_two_verified_routes_in_order(self):
        self.assertEqual(config.FILE_RULE_ALL, "all_files")
        self.assertEqual(config.FILE_RULE_PDF_ONLY, "pdf_only")
        self.assertIsInstance(config.VERIFIED_ROUTES, tuple)
        self.assertEqual(len(config.VERIFIED_ROUTES), 2)
        self.assertEqual(
            [dataclasses.astuple(r) for r in config.VERIFIED_ROUTES],
            [
                ("master-schedule", SRC, MASTER, SRC_COL, MASTER_COL, "all_files",
                 "Work Request #", "WR"),
                ("blank-copy", SRC, BLANK, SRC_COL, BLANK_COL, "pdf_only",
                 "Work Request #", "Work Request #"),
            ],
        )
        master, blank = config.VERIFIED_ROUTES
        self.assertEqual(
            (master.source_match_column_title, master.target_match_column_title,
             blank.source_match_column_title, blank.target_match_column_title),
            ("Work Request #", "WR", "Work Request #", "Work Request #"),
        )
        for route in config.VERIFIED_ROUTES:
            for field in ("source_sheet_id", "target_sheet_id",
                          "source_match_column_id", "target_match_column_id"):
                self.assertIs(type(getattr(route, field)), int)
        self.assertFalse(hasattr(config, "MATCH_COLUMN_TITLE"))

    def test_route_is_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.VERIFIED_ROUTES[0].target_sheet_id = BLANK
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.VERIFIED_ROUTES[1].file_rule = FILE_RULE_ALL

    def test_legacy_single_route_names_are_gone(self):
        for name in ("VERIFIED_ROUTING", "RETIRED_TARGET_SHEET_IDS", "SOURCE_SHEET_ID",
                     "TARGET_SHEET_ID", "SOURCE_MATCH_COLUMN_ID", "TARGET_MATCH_COLUMN_ID",
                     "validate_routing"):
            self.assertFalse(hasattr(config, name), name)

    def test_verified_routes_are_valid(self):
        for route in config.VERIFIED_ROUTES:
            self.assertEqual(config.validate_route(route), [])
        self.assertEqual(config.validate_route(MASTER_ROUTE), [])
        self.assertEqual(config.validate_route(BLANK_ROUTE), [])

    def test_file_rule_is_bound_to_the_target(self):
        problems = config.validate_route(dataclasses.replace(BLANK_ROUTE, file_rule=FILE_RULE_ALL))
        self.assertTrue(any("file rule" in p for p in problems), problems)
        problems = config.validate_route(dataclasses.replace(MASTER_ROUTE, file_rule=FILE_RULE_PDF_ONLY))
        self.assertTrue(any("file rule" in p for p in problems), problems)

    def test_unknown_file_rule_is_rejected_even_when_pinned(self):
        self.assertEqual(config.KNOWN_FILE_RULES, ("all_files", "pdf_only"))
        for rule in ("pdf-only", None, "", "PDF_ONLY", ["pdf_only"]):
            with self.subTest(rule=rule):
                typo = dataclasses.replace(BLANK_ROUTE, file_rule=rule)
                with mock.patch.object(config, "VERIFIED_ROUTES", (MASTER_ROUTE, typo)):
                    problems = config.validate_route(typo)
                self.assertTrue(any("unknown file rule" in p for p in problems), problems)

    def test_right_ids_with_wrong_title_are_rejected(self):
        problems = config.validate_route(
            dataclasses.replace(MASTER_ROUTE, target_match_column_title="Work Request #"))
        self.assertTrue(any("target_match_column_title" in p for p in problems), problems)

    def test_unknown_target_same_sheet_and_wrong_columns_are_rejected(self):
        unknown = config.validate_route(dataclasses.replace(BLANK_ROUTE, target_sheet_id=999))
        self.assertTrue(any("not a verified target" in p for p in unknown), unknown)
        same = config.validate_route(dataclasses.replace(BLANK_ROUTE, target_sheet_id=SRC))
        self.assertTrue(any("same sheet" in p for p in same), same)
        for change in (
            {"target_match_column_id": MASTER_COL},   # the other route's column
            {"source_match_column_id": BLANK_COL},
            {"source_sheet_id": 123},
            {"name": "master-schedule"},
            {"target_match_column_title": "WR"},
            {"source_match_column_title": "WR"},
            {"target_match_column_title": "work request #"},   # exact, case-sensitive
        ):
            with self.subTest(change=change):
                self.assertTrue(config.validate_route(dataclasses.replace(BLANK_ROUTE, **change)))

    def test_non_integer_ids_are_rejected(self):
        for value in (str(BLANK), float(BLANK), True, None, "abc"):
            with self.subTest(value=value):
                problems = config.validate_route(dataclasses.replace(BLANK_ROUTE, target_sheet_id=value))
                self.assertTrue(any("must be an integer" in p for p in problems), problems)
        self.assertTrue(config.validate_route(
            ("blank-copy", SRC, BLANK, SRC_COL, BLANK_COL, "pdf_only", WR_TITLE, WR_TITLE)))

    def test_routing_env_vars_set_reports_only_non_blank_names(self):
        env = {"SOURCE_SHEET_ID": "1", "TARGET_SHEET_ID": "  ", "TARGET_MATCH_COLUMN_ID": "2",
               "SMARTSHEET_API_KEY": "x"}
        self.assertEqual(config.routing_env_vars_set(env),
                         ["SOURCE_SHEET_ID", "TARGET_MATCH_COLUMN_ID"])
        self.assertEqual(config.routing_env_vars_set({}), [])


class MainTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GITHUB_ACTIONS", None)  # local-run behaviour unless a test sets it
        for name in ROUTING_VARS:
            os.environ.pop(name, None)
        patcher = mock.patch.object(main, "SMARTSHEET_API_KEY", GOOD_KEY)
        patcher.start()
        self.addCleanup(patcher.stop)

        # One mock syncer per route; per-route results/side effects keyed by route name.
        self.syncers = {}
        self.results = {"master-schedule": {"errors": 0}, "blank-copy": {"errors": 0}}
        self.init_errors = {}
        self.order = []

        def build(api_key, route, temp_folder):
            self.order.append(route)
            if route.name in self.init_errors:
                raise self.init_errors[route.name]
            syncer = mock.MagicMock(name=f"syncer-{route.name}")
            result = self.results[route.name]
            if isinstance(result, BaseException) or (
                    isinstance(result, type) and issubclass(result, BaseException)):
                syncer.sync_attachments.side_effect = result
            else:
                syncer.sync_attachments.return_value = result
            self.syncers[route.name] = syncer
            return syncer

        patcher = mock.patch.object(main, "AttachmentSyncer", side_effect=build)
        self.syncer_cls = patcher.start()
        self.addCleanup(patcher.stop)

    def run_main(self, argv):
        with self.assertLogs(level="INFO") as logs:
            code = main.main(argv)
        return code, "\n".join(logs.output)

    def test_both_routes_run_master_schedule_first(self):
        code, text = self.run_main([])
        self.assertEqual(code, 0)
        self.assertEqual(self.order, list(config.VERIFIED_ROUTES))
        self.assertEqual([r.name for r in self.order], ["master-schedule", "blank-copy"])
        for syncer in self.syncers.values():
            syncer.sync_attachments.assert_called_once_with(apply=False)
        self.assertIn("DRY RUN (default", text)
        self.assertIn("Route master-schedule: completed with no errors", text)
        self.assertIn("Route blank-copy: completed with no errors", text)

    def test_apply_flag_reaches_both_routes(self):
        self.assertEqual(self.run_main(["--apply"])[0], 0)
        self.assertEqual(set(self.syncers), {"master-schedule", "blank-copy"})
        for syncer in self.syncers.values():
            syncer.sync_attachments.assert_called_once_with(apply=True)

    def test_github_actions_without_mode_flag_fails_before_any_api_call(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        with self.assertLogs(level="ERROR") as logs:
            code = main.main([])
        self.assertEqual(code, 1)
        self.syncer_cls.assert_not_called()
        self.assertIn("must pass --dry-run or --apply", "\n".join(logs.output))

    def test_github_actions_with_explicit_mode_runs(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        self.assertEqual(self.run_main(["--dry-run"])[0], 0)
        self.syncers["blank-copy"].sync_attachments.assert_called_with(apply=False)
        self.assertEqual(self.run_main(["--apply"])[0], 0)
        self.syncers["master-schedule"].sync_attachments.assert_called_with(apply=True)
        self.syncers["blank-copy"].sync_attachments.assert_called_with(apply=True)

    def test_dry_run_and_apply_are_mutually_exclusive(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as ctx:
            main.main(["--dry-run", "--apply"])
        self.assertNotEqual(ctx.exception.code, 0)
        self.syncer_cls.assert_not_called()

    def test_routing_env_vars_are_ignored_with_one_warning(self):
        os.environ.update({
            "SOURCE_SHEET_ID": str(BLANK),
            "TARGET_SHEET_ID": "999",
            "SOURCE_MATCH_COLUMN_ID": "1",
            "TARGET_MATCH_COLUMN_ID": "2",
        })
        with self.assertLogs(level="WARNING") as logs:
            code = main.main(["--apply"])
        self.assertEqual(code, 0)
        self.assertEqual(self.order, [MASTER_ROUTE, BLANK_ROUTE])
        warnings = [r for r in logs.records if "Ignoring routing environment variables" in r.getMessage()]
        self.assertEqual(len(warnings), 1)
        for name in ROUTING_VARS:
            self.assertIn(name, warnings[0].getMessage())

    def test_no_warning_without_routing_env_vars(self):
        _, text = self.run_main([])
        self.assertNotIn("Ignoring routing environment variables", text)

    def test_missing_api_key_exits_1_and_key_is_never_logged(self):
        with mock.patch.object(main, "SMARTSHEET_API_KEY", None):
            self.assertEqual(self.run_main([])[0], 1)
        self.syncer_cls.assert_not_called()
        _, text = self.run_main([])
        self.assertNotIn(GOOD_KEY, text)

    def test_errors_in_either_route_give_nonzero_exit(self):
        for name in ("master-schedule", "blank-copy"):
            with self.subTest(route=name):
                self.results = {"master-schedule": {"errors": 0}, "blank-copy": {"errors": 0}}
                self.results[name] = {"errors": 1, "uploads_confirmed": 40}
                self.order = []
                code, text = self.run_main(["--apply"])
                self.assertEqual(code, 1)
                self.assertEqual(len(self.order), 2)   # the other route still ran
                self.assertIn(f"Route {name}: 1 unresolved error(s)", text)

    def test_expected_skips_alone_exit_0(self):
        self.results["blank-copy"] = {
            "errors": 0, "wr_unmatched": 12, "source_wr_invalid": 5, "wr_ambiguous": 2,
        }
        self.assertEqual(self.run_main(["--apply"])[0], 0)

    def test_failure_in_first_route_does_not_stop_the_second(self):
        cases = (
            ("sync RoutingError", "results", main.RoutingError("column missing")),
            ("sync RuntimeError", "results", RuntimeError("boom")),
            ("init RoutingError", "init", main.RoutingError("bad route")),
            ("init RuntimeError", "init", RuntimeError("adapters")),
        )
        for label, where, exc in cases:
            with self.subTest(case=label):
                self.results = {"master-schedule": {"errors": 0}, "blank-copy": {"errors": 0}}
                self.init_errors = {}
                self.syncers = {}
                self.order = []
                if where == "init":
                    self.init_errors["master-schedule"] = exc
                else:
                    self.results["master-schedule"] = exc
                code, text = self.run_main(["--apply"])
                self.assertEqual(code, 1)
                self.assertEqual([r.name for r in self.order], ["master-schedule", "blank-copy"])
                self.syncers["blank-copy"].sync_attachments.assert_called_once_with(apply=True)
                self.assertIn("Route master-schedule:", text)
                self.assertIn("Route blank-copy: completed with no errors", text)

    def test_failure_in_second_route_exits_1(self):
        self.results["blank-copy"] = main.RoutingError("target column renamed")
        code, text = self.run_main(["--apply"])
        self.assertEqual(code, 1)
        self.assertIn("Route master-schedule: completed with no errors", text)
        self.assertIn("Route blank-copy: routing check failed", text)

    def test_keyboard_interrupt_exits_130_and_stops_everything(self):
        self.results["master-schedule"] = KeyboardInterrupt
        self.assertEqual(self.run_main(["--apply"])[0], 130)
        self.assertEqual([r.name for r in self.order], ["master-schedule"])

    def test_sdk_loggers_are_quiet(self):
        self.assertEqual(logging.getLogger("smartsheet").level, logging.WARNING)
        self.assertEqual(logging.getLogger("urllib3").level, logging.WARNING)


if __name__ == "__main__":
    unittest.main()
