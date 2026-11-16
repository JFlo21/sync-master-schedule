"""
Routing configuration and CLI/exit-status tests. Offline; the syncer is mocked.
"""
import importlib
import logging
import os
import unittest
from unittest import mock

from src import config

SRC = 1553121697288068
TGT = 573385022328708
SRC_COL = 8022925527175044
TGT_COL = 7068369005875076
OLD_TGT = 4850043816202116
OLD_TGT_COL = 1437881091182468
GOOD_KEY = "test-key-not-real-0123456789"

# main.py calls load_dotenv() at import; never let tests read a real .env.
with mock.patch("dotenv.load_dotenv"):
    main = importlib.import_module("main")


class RoutingConfigTests(unittest.TestCase):
    def test_verified_routing_is_the_blank_copy(self):
        self.assertEqual(
            dict(config.VERIFIED_ROUTING),
            {
                "source_sheet_id": SRC,
                "target_sheet_id": TGT,
                "source_match_column_id": SRC_COL,
                "target_match_column_id": TGT_COL,
            },
        )
        self.assertIn(OLD_TGT, config.RETIRED_TARGET_SHEET_IDS)
        self.assertEqual(config.MATCH_COLUMN_TITLE, "Work Request #")
        self.assertEqual(config.validate_routing(SRC, TGT, SRC_COL, TGT_COL), [])
        self.assertEqual(config.validate_routing(str(SRC), f" {TGT} ", SRC_COL, TGT_COL), [])

    def test_defaults_without_env_are_verified_ints(self):
        names = ("SOURCE_SHEET_ID", "TARGET_SHEET_ID", "SOURCE_MATCH_COLUMN_ID", "TARGET_MATCH_COLUMN_ID")
        clean_env = {k: v for k, v in os.environ.items() if k not in names}
        try:
            with mock.patch.dict(os.environ, clean_env, clear=True):
                fresh = importlib.reload(config)
                values = (fresh.SOURCE_SHEET_ID, fresh.TARGET_SHEET_ID,
                          fresh.SOURCE_MATCH_COLUMN_ID, fresh.TARGET_MATCH_COLUMN_ID)
        finally:
            importlib.reload(config)
        self.assertEqual(values, (SRC, TGT, SRC_COL, TGT_COL))
        self.assertTrue(all(type(v) is int for v in values))

    def test_old_target_is_rejected(self):
        problems = config.validate_routing(SRC, OLD_TGT, SRC_COL, OLD_TGT_COL)
        text = " | ".join(problems)
        self.assertIn("retired", text)
        self.assertIn(f"expected {TGT}, effective {OLD_TGT}", text)
        self.assertIn(f"expected {TGT_COL}, effective {OLD_TGT_COL}", text)

    def test_same_sheet_and_mismatched_columns_are_rejected(self):
        self.assertTrue(any("same sheet" in p for p in config.validate_routing(SRC, SRC, SRC_COL, TGT_COL)))
        self.assertTrue(config.validate_routing(SRC, TGT, TGT_COL, SRC_COL))
        self.assertTrue(config.validate_routing(SRC, TGT, SRC_COL, 123))
        self.assertTrue(config.validate_routing(SRC, TGT, SRC_COL, "abc"))
        self.assertTrue(config.validate_routing(SRC, TGT, True, TGT_COL))


class MainTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GITHUB_ACTIONS", None)  # local-run behaviour unless a test sets it
        for name, value in (
            ("SMARTSHEET_API_KEY", GOOD_KEY),
            ("SOURCE_SHEET_ID", SRC),
            ("TARGET_SHEET_ID", TGT),
            ("SOURCE_MATCH_COLUMN_ID", SRC_COL),
            ("TARGET_MATCH_COLUMN_ID", TGT_COL),
        ):
            patcher = mock.patch.object(main, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(main, "AttachmentSyncer")
        self.syncer_cls = patcher.start()
        self.addCleanup(patcher.stop)
        self.syncer = self.syncer_cls.return_value
        self.syncer.sync_attachments.return_value = {"errors": 0}

    def run_main(self, argv):
        with self.assertLogs(level="INFO") as logs:
            code = main.main(argv)
        return code, "\n".join(logs.output)

    def test_no_flag_defaults_to_dry_run(self):
        code, text = self.run_main([])
        self.assertEqual(code, 0)
        self.syncer.sync_attachments.assert_called_once_with(apply=False)
        self.assertIn("DRY RUN (default", text)

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
        self.syncer.sync_attachments.assert_called_with(apply=False)
        self.assertEqual(self.run_main(["--apply"])[0], 0)
        self.syncer.sync_attachments.assert_called_with(apply=True)

    def test_explicit_dry_run_and_apply(self):
        self.assertEqual(self.run_main(["--dry-run"])[0], 0)
        self.syncer.sync_attachments.assert_called_with(apply=False)
        self.assertEqual(self.run_main(["--apply"])[0], 0)
        self.syncer.sync_attachments.assert_called_with(apply=True)

    def test_dry_run_and_apply_are_mutually_exclusive(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as ctx:
            main.main(["--dry-run", "--apply"])
        self.assertNotEqual(ctx.exception.code, 0)
        self.syncer_cls.assert_not_called()

    def test_bad_routing_exits_1_before_syncer_is_built(self):
        with mock.patch.object(main, "TARGET_SHEET_ID", OLD_TGT), \
                mock.patch.object(main, "TARGET_MATCH_COLUMN_ID", OLD_TGT_COL):
            code, text = self.run_main(["--apply"])
        self.assertEqual(code, 1)
        self.syncer_cls.assert_not_called()
        self.assertIn("retired", text)

    def test_missing_api_key_exits_1_and_key_is_never_logged(self):
        with mock.patch.object(main, "SMARTSHEET_API_KEY", None):
            self.assertEqual(self.run_main([])[0], 1)
        self.syncer_cls.assert_not_called()
        _, text = self.run_main([])
        self.assertNotIn(GOOD_KEY, text)

    def test_errors_give_nonzero_exit(self):
        self.syncer.sync_attachments.return_value = {"errors": 1, "uploads_confirmed": 40}
        self.assertEqual(self.run_main(["--apply"])[0], 1)

    def test_expected_skips_alone_exit_0(self):
        self.syncer.sync_attachments.return_value = {
            "errors": 0, "wr_unmatched": 12, "source_wr_invalid": 5, "wr_ambiguous": 2,
        }
        self.assertEqual(self.run_main(["--apply"])[0], 0)

    def test_routing_error_and_fatal_error_exit_1(self):
        for exc in (main.RoutingError("column missing"), RuntimeError("boom")):
            with self.subTest(exc=exc):
                self.syncer.sync_attachments.side_effect = exc
                self.assertEqual(self.run_main(["--apply"])[0], 1)

    def test_keyboard_interrupt_exits_130(self):
        self.syncer.sync_attachments.side_effect = KeyboardInterrupt
        self.assertEqual(self.run_main(["--apply"])[0], 130)

    def test_sdk_loggers_are_quiet(self):
        self.assertEqual(logging.getLogger("smartsheet").level, logging.WARNING)
        self.assertEqual(logging.getLogger("urllib3").level, logging.WARNING)


if __name__ == "__main__":
    unittest.main()
