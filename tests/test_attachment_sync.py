"""
Focused tests for the PDF attachment sync. Offline: Smartsheet clients, downloads
and time are all mocked; no credentials, no network, no real sleeping.
"""
import logging
import os
import tempfile
import unittest
from collections import defaultdict
from types import SimpleNamespace as NS
from unittest import mock

import requests
import smartsheet
import urllib3
from requests.structures import CaseInsensitiveDict
from smartsheet.exceptions import (
    ApiError,
    RateLimitExceededError,
    ServerTimeoutExceededError,
    UnexpectedRequestError,
)
from urllib3.util.retry import Retry

from src import attachment_sync
from src.attachment_sync import (
    AttachmentSyncer,
    RoutingError,
    classify_pdf_metadata,
    disable_transport_retries,
    has_pdf_signature,
    has_unsupported_filename_chars,
    normalize_work_request,
)

SRC = 1553121697288068
TGT = 573385022328708
SRC_COL = 8022925527175044
TGT_COL = 7068369005875076
OLD_TGT = 4850043816202116
OLD_TGT_COL = 1437881091182468
OTHER_COL = 111
API_KEY = "test-key-not-real-0123456789"

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
HTML_BYTES = b"<!DOCTYPE html><html><body>Access denied</body></html>"
SIGNED_MARKER = "SIGNED-TOKEN"

ALLOWED_SDK_CALLS = {
    "Sheets.get_sheet",
    "Attachments.list_row_attachments",
    "Attachments.get_attachment",
    "Attachments.attach_file_to_row",
}
CLIENT_CONFIG_CALLS = {"errors_as_exceptions"}


def api_error(cls=ApiError, code=1004, message="Not authorized", should_retry=None):
    error = NS(result=NS(error_code=code, message=message))
    if cls is ApiError:
        return ApiError(error, message, bool(should_retry))
    return cls(error, message)


def make_sheet(sheet_id, col_id, values, row_base, title="Work Request #", total=None):
    rows = [
        NS(
            id=row_base + i,
            cells=[NS(column_id=OTHER_COL, value=f"row {i}"), NS(column_id=col_id, value=v)],
        )
        for i, v in enumerate(values)
    ]
    return NS(
        id=sheet_id,
        columns=[NS(id=OTHER_COL, title="Row ID"), NS(id=col_id, title=title)],
        rows=rows,
        total_row_count=len(rows) if total is None else total,
    )


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, content, status=200, headers=None):
        self.content = content
        self.status_code = status
        self.headers = CaseInsensitiveDict(
            {"Content-Length": str(len(content))} if headers is None else headers
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            resp = requests.Response()
            resp.status_code = self.status_code
            raise requests.exceptions.HTTPError(
                f"{self.status_code} Error for url: https://signed.example.invalid/x?{SIGNED_MARKER}",
                response=resp,
            )

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


class FakeWorld:
    """In-memory Smartsheet + download host."""

    def __init__(self, clock, source_values, target_values, source_title="Work Request #",
                 target_title="Work Request #"):
        self.clock = clock
        self.sheets = {
            SRC: make_sheet(SRC, SRC_COL, source_values, 1000, source_title),
            TGT: make_sheet(TGT, TGT_COL, target_values, 5000, target_title),
        }
        self.row_attachments = defaultdict(list)
        self.details = {}
        self.downloads = {}
        self.uploads = []
        self.upload_starts = []
        self.list_failures = defaultdict(list)
        self.incomplete_listing = set()
        self.missing_total = set()
        self.stored_names = {}  # sent name -> name the server reports storing
        self.upload_effects = []
        self.download_failures = []
        self.http_get_urls = []
        self._next_id = 90000
        self._url_counter = 0

    def _new_id(self):
        self._next_id += 1
        return self._next_id

    def add_file(self, sheet_id, row_id, name, mime="application/pdf", content=PDF_BYTES,
                 attachment_type="FILE", detail_mime="same", parent_type="ROW"):
        att_id = self._new_id()
        att = NS(id=att_id, name=name, mime_type=mime, attachment_type=attachment_type,
                 parent_type=parent_type)
        self.row_attachments[(sheet_id, row_id)].append(att)
        self.details[att_id] = {
            "mime": mime if detail_mime == "same" else detail_mime,
            "content": content,
        }
        return att

    # --- SDK fakes -------------------------------------------------------
    def get_sheet(self, sheet_id):
        return self.sheets[sheet_id]

    def list_row_attachments(self, sheet_id, row_id, include_all=None):
        key = (sheet_id, row_id)
        if self.list_failures[key]:
            raise self.list_failures[key].pop(0)
        items = list(self.row_attachments[key])
        total = len(items) + 1 if key in self.incomplete_listing else len(items)
        if key in self.missing_total:
            total = None
        return NS(data=items, total_count=total)

    def get_attachment(self, sheet_id, attachment_id):
        detail = self.details[attachment_id]
        self._url_counter += 1
        url = f"https://signed.example.invalid/dl/{attachment_id}?{SIGNED_MARKER}-{self._url_counter}"
        self.downloads[url] = detail["content"]
        return NS(id=attachment_id, mime_type=detail["mime"], url=url)

    def attach_file_to_row(self, sheet_id, row_id, file_tuple):
        name, fh, mime = file_tuple
        data = fh.read()
        self.upload_starts.append(self.clock.now)
        store, exc = self.upload_effects.pop(0) if self.upload_effects else (True, None)
        stored_name = self.stored_names.get(name, name)
        if store:
            self.row_attachments[(sheet_id, row_id)].append(
                NS(id=self._new_id(), name=stored_name, mime_type=mime, attachment_type="FILE",
                   parent_type="ROW")
            )
            self.uploads.append((sheet_id, row_id, name, data, mime))
        if exc is not None:
            raise exc
        return NS(message="SUCCESS", result=NS(name=stored_name))

    def http_get(self, url, stream=False, timeout=None):
        self.http_get_urls.append(url)
        if self.download_failures:
            failure = self.download_failures.pop(0)
            if isinstance(failure, int):
                return FakeResponse(b"", status=failure)
            if failure == "truncate":   # body shorter than the declared Content-Length
                content = self.downloads[url]
                return FakeResponse(content[:10], headers={"Content-Length": str(len(content))})
            if failure == "gzip":       # Content-Length refers to the encoded body
                return FakeResponse(self.downloads[url],
                                    headers={"Content-Length": "5", "Content-Encoding": "gzip"})
            raise failure
        return FakeResponse(self.downloads[url])


class SmartsheetFactory:
    """Stands in for smartsheet.Smartsheet; records every client and its kwargs."""

    def __init__(self):
        self.created = []
        self.with_session = True

    def __call__(self, *args, **kwargs):
        client = mock.MagicMock(name=f"smartsheet-client-{len(self.created)}")
        if self.with_session:
            # Same shape as SDK 4.4.0 pinned_session(): POST-retrying https adapter
            client._session = NS(adapters={
                "https://": NS(max_retries=Retry(
                    total=1, allowed_methods=Retry.DEFAULT_ALLOWED_METHODS.union(["POST"]))),
                "http://": NS(max_retries=Retry(total=0, read=False)),
            })
        self.created.append((client, args, kwargs))
        return client


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.temp_folder = os.path.join(tmp.name, "downloads")
        self.clock = FakeClock()
        self.factory = SmartsheetFactory()
        self.requests_get = mock.MagicMock(name="requests.get")
        for target, attr, new in (
            (attachment_sync.time, "sleep", self.clock.sleep),
            (attachment_sync.time, "monotonic", self.clock.monotonic),
            (attachment_sync.smartsheet, "Smartsheet", self.factory),
            (attachment_sync.requests, "get", self.requests_get),
        ):
            patcher = mock.patch.object(target, attr, new)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_world(self, source_values, target_values, **kwargs):
        return FakeWorld(self.clock, source_values, target_values, **kwargs)

    def make_syncer(self, world, **kwargs):
        syncer = AttachmentSyncer(
            API_KEY, SRC, TGT, SRC_COL, TGT_COL, temp_folder=self.temp_folder, **kwargs
        )
        read, upload = self.factory.created[-2][0], self.factory.created[-1][0]
        read.Sheets.get_sheet.side_effect = world.get_sheet
        read.Attachments.list_row_attachments.side_effect = world.list_row_attachments
        read.Attachments.get_attachment.side_effect = world.get_attachment
        upload.Attachments.attach_file_to_row.side_effect = world.attach_file_to_row
        self.requests_get.side_effect = world.http_get
        self.read, self.upload = read, upload
        return syncer

    def sdk_calls(self):
        names = set()
        for client, _, _ in self.factory.created:
            for call in client.mock_calls:
                names.add(call[0])
        return names - CLIENT_CONFIG_CALLS

    def assert_only_allowed_calls(self, matched_target_rows):
        self.assertLessEqual(self.sdk_calls(), ALLOWED_SDK_CALLS)
        # Uploads only through the upload client, only to verified sheet + matched rows
        self.read.Attachments.attach_file_to_row.assert_not_called()
        for call in self.upload.Attachments.attach_file_to_row.call_args_list:
            sheet_id, row_id = call.args[0], call.args[1]
            self.assertEqual(sheet_id, TGT)
            self.assertIn(row_id, matched_target_rows)
        upload_other = {c[0] for c in self.upload.mock_calls} - CLIENT_CONFIG_CALLS
        self.assertLessEqual(upload_other, {"Attachments.attach_file_to_row"})

    def temp_files_left(self):
        if not os.path.isdir(self.temp_folder):
            return []
        return os.listdir(self.temp_folder)


# ---------------------------------------------------------------- pure helpers


class NormalizeWorkRequestTests(unittest.TestCase):
    def test_numeric_and_string_forms_are_equivalent(self):
        for value in (92873293, 92873293.0, "92873293", " 92873293.0 ", "92873293.00"):
            with self.subTest(value=value):
                self.assertEqual(normalize_work_request(value), (92873293, "valid"))

    def test_rejects_invalid_values(self):
        for value in (
            True, False, 0, -5, 1.5, -1.0, 0.0, float("nan"), float("inf"), float("-inf"),
            2.0 ** 60, "1.5", "92873293.5", "WR-01", "WR-02", "CANCELED", "Canceled",
            "1e3", "+5", "-5", "1,234", "0", "007", "12 34", "١٢٣", "92873293.", ".0",
            [92873293], object(),
        ):
            with self.subTest(value=value):
                self.assertEqual(normalize_work_request(value), (None, "invalid"))

    def test_blank_values(self):
        for value in (None, "", "   ", "\t"):
            with self.subTest(value=value):
                self.assertEqual(normalize_work_request(value), (None, "blank"))


class ClassifyPdfMetadataTests(unittest.TestCase):
    def test_decisions(self):
        cases = [
            ("a.pdf", "application/pdf", "pdf"),
            ("a.pdf", " Application/PDF; charset=binary ", "pdf"),
            ("no-extension", "application/pdf", "pdf"),
            ("SCAN.PDF", None, "pdf"),
            ("Scan.Pdf ", "", "pdf"),
            ("scan.pdf", "application/octet-stream", "pdf"),
            ("scan.pdf", "binary/octet-stream", "pdf"),
            ("blob", "application/octet-stream", "not_pdf"),
            ("blob", None, "not_pdf"),
            ("Adobe report.xlsx",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "not_pdf"),
            ("photo.png", "image/png", "not_pdf"),
            ("Adobe Acrobat notes.docx", None, "not_pdf"),
            ("fake.pdf", "image/png", "conflict"),
            ("fake.pdf", "text/html", "conflict"),
        ]
        for name, mime, expected in cases:
            with self.subTest(name=name, mime=mime):
                self.assertEqual(classify_pdf_metadata(name, mime)[0], expected)


class PdfSignatureTests(unittest.TestCase):
    def check(self, content):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(content)
        self.addCleanup(os.remove, fh.name)
        return has_pdf_signature(fh.name)

    def test_signatures(self):
        self.assertTrue(self.check(PDF_BYTES))
        self.assertTrue(self.check(b"\xef\xbb\xbf \r\n%PDF-1.4 rest"))
        self.assertTrue(self.check(b"\x00junk-prefix-bytes" * 20 + b"%PDF-1.5 rest"))
        self.assertFalse(self.check(HTML_BYTES))
        self.assertFalse(self.check(b""))
        self.assertFalse(self.check(b"PK\x03\x04 renamed xlsx"))
        self.assertFalse(self.check(b"%PDF"))

    def test_signature_beyond_first_1024_bytes_is_rejected(self):
        self.assertFalse(self.check(b"x" * 1024 + b"%PDF-1.7"))

    def test_missing_file_is_not_a_pdf(self):
        self.assertFalse(has_pdf_signature(os.path.join(tempfile.gettempdir(), "no-such-file.pdf")))


# --------------------------------------------------------------- construction


class ConstructionTests(SyncTestCase):
    def test_upload_client_disables_sdk_retries(self):
        self.make_syncer(self.make_world([], []))
        self.assertEqual(len(self.factory.created), 2)
        (_, read_args, read_kwargs), (_, up_args, up_kwargs) = self.factory.created
        self.assertNotIn("max_retry_time", read_kwargs)
        self.assertEqual(up_kwargs.get("max_retry_time"), 0)
        self.assertEqual(read_args, (API_KEY,))
        self.assertEqual(up_args, (API_KEY,))

    def test_wrong_routing_rejected_before_clients_exist(self):
        bad_routings = [
            (SRC, OLD_TGT, SRC_COL, OLD_TGT_COL),   # old configured target
            (SRC, TGT, SRC_COL, OLD_TGT_COL),       # old target column
            (SRC, SRC, SRC_COL, TGT_COL),           # same-sheet routing
            (TGT, SRC, TGT_COL, SRC_COL),           # swapped
            (SRC, 999, SRC_COL, TGT_COL),           # unknown target
            (SRC, "not-a-number", SRC_COL, TGT_COL),
        ]
        for routing in bad_routings:
            with self.subTest(routing=routing):
                with self.assertRaises(RoutingError):
                    AttachmentSyncer(API_KEY, *routing, temp_folder=self.temp_folder)
        self.assertEqual(self.factory.created, [])

    def test_temp_folder_not_created_at_init(self):
        self.make_syncer(self.make_world([], []))
        self.assertFalse(os.path.exists(self.temp_folder))


# ------------------------------------------------------------------ preflight


class PreflightTests(SyncTestCase):
    def run_expecting_routing_error(self, world):
        syncer = self.make_syncer(world)
        with self.assertRaises(RoutingError):
            syncer.sync_attachments(apply=True)
        self.upload.Attachments.attach_file_to_row.assert_not_called()
        self.read.Attachments.list_row_attachments.assert_not_called()
        return syncer

    def test_api_returns_different_sheet(self):
        world = self.make_world(["1"], ["1"])
        world.sheets[TGT].id = OLD_TGT
        self.run_expecting_routing_error(world)

    def test_missing_match_column(self):
        world = self.make_world(["1"], ["1"])
        world.sheets[TGT].columns = [NS(id=OTHER_COL, title="Row ID")]
        self.run_expecting_routing_error(world)

    def test_match_column_with_wrong_title(self):
        self.run_expecting_routing_error(self.make_world(["1"], ["1"], target_title="Scope #"))

    def test_source_column_with_wrong_title(self):
        self.run_expecting_routing_error(self.make_world(["1"], ["1"], source_title="Row ID"))

    def test_incomplete_sheet_load(self):
        world = self.make_world(["1"], ["1"])
        world.sheets[SRC].total_row_count = 5
        self.run_expecting_routing_error(world)


# ------------------------------------------------------------------- matching


class MatchingTests(SyncTestCase):
    def test_numeric_and_text_keys_match(self):
        world = self.make_world([92873293.0], [" 92873293 "])
        world.add_file(SRC, 1000, "a.pdf")
        syncer = self.make_syncer(world)
        stats = syncer.sync_attachments(apply=False)
        self.assertEqual(stats["wr_matched"], 1)
        self.assertEqual(stats["pdf_planned"], 1)

    def test_placeholders_are_skipped_not_matched(self):
        source = ["WR-01", "WR-01", "WR-02", "CANCELED", "Canceled", None, "", 1.5, True]
        target = ["WR-01", "WR-02", "CANCELED", "Canceled"]
        world = self.make_world(source, target)
        syncer = self.make_syncer(world)
        stats = syncer.sync_attachments(apply=True)
        self.assertEqual(stats["wr_matched"], 0)
        self.assertEqual(stats["wr_ambiguous"], 0)
        self.assertEqual(stats["source_wr_invalid"], 7)
        self.assertEqual(stats["source_rows_blank"], 2)
        self.assertEqual(stats["target_wr_invalid"], 4)
        self.assertEqual(stats["errors"], 0)
        self.read.Attachments.list_row_attachments.assert_not_called()

    def test_ambiguous_wr_in_source_or_target_is_skipped_and_reported(self):
        # 111 duplicated in source, 222 duplicated in target, 333 clean
        world = self.make_world([111, "111.0", 222, 333], [111, 222, "222", 333])
        for row in (1000, 1001, 1002, 1003):
            world.add_file(SRC, row, f"row{row}.pdf")
        syncer = self.make_syncer(world)
        with self.assertLogs("src.attachment_sync", level="WARNING") as logs:
            stats = syncer.sync_attachments(apply=True)
        self.assertEqual(stats["wr_ambiguous"], 2)
        self.assertEqual(stats["wr_matched"], 1)
        text = "\n".join(logs.output)
        self.assertIn("[1000, 1001]", text)   # source rows for WR 111
        self.assertIn("[5001, 5002]", text)   # target rows for WR 222
        self.assertEqual([u[1] for u in world.uploads], [5003])
        self.assert_only_allowed_calls({5003})

    def test_unmatched_wr_is_skipped_without_creating_rows(self):
        world = self.make_world([111, 444], [111])
        world.add_file(SRC, 1000, "a.pdf")
        world.add_file(SRC, 1001, "b.pdf")
        syncer = self.make_syncer(world)
        stats = syncer.sync_attachments(apply=True)
        self.assertEqual(stats["wr_unmatched"], 1)
        self.assertEqual(stats["uploads_confirmed"], 1)
        self.assertEqual(stats["errors"], 0)
        self.assert_only_allowed_calls({5000})

    def test_empty_target_is_a_reported_noop(self):
        world = self.make_world([111, 222], [])
        world.add_file(SRC, 1000, "a.pdf")
        syncer = self.make_syncer(world)
        with self.assertLogs("src.attachment_sync", level="INFO") as logs:
            stats = syncer.sync_attachments(apply=True)
        self.assertTrue(any("nothing to do" in line for line in logs.output))
        self.assertEqual(stats["wr_unmatched"], 2)
        self.assertEqual(stats["errors"], 0)
        self.read.Attachments.list_row_attachments.assert_not_called()
        self.upload.Attachments.attach_file_to_row.assert_not_called()


# ------------------------------------------------------------- PDF eligibility


def add_mixed_row(world, row_id):
    world.add_file(SRC, row_id, "a.pdf")
    world.add_file(SRC, row_id, "B.PDF", mime="application/octet-stream")
    world.add_file(SRC, row_id, "Adobe costs.xlsx",
                   mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    world.add_file(SRC, row_id, "photo.png", mime="image/png")
    world.add_file(SRC, row_id, "Drawing link", mime=None, attachment_type="LINK")
    world.add_file(SRC, row_id, "box.pdf", mime=None, attachment_type="BOX_COM")
    world.add_file(SRC, row_id, "fake.pdf", mime="image/png", content=b"\x89PNG")
    world.add_file(SRC, row_id, "no-mime-in-listing", mime=None, detail_mime="application/pdf")
    world.add_file(SRC, row_id, "missing.pdf", mime=None, detail_mime=None)


MIXED_EXPECTED_UPLOADS = ["a.pdf", "B.PDF", "no-mime-in-listing", "missing.pdf"]


class PdfEligibilityTests(SyncTestCase):
    def test_only_valid_pdf_files_are_copied(self):
        world = self.make_world([111], [111])
        add_mixed_row(world, 1000)
        syncer = self.make_syncer(world)
        stats = syncer.sync_attachments(apply=True)

        self.assertEqual([u[2] for u in world.uploads], MIXED_EXPECTED_UPLOADS)
        for sheet_id, row_id, name, data, mime in world.uploads:
            self.assertEqual((sheet_id, row_id, mime, data), (TGT, 5000, "application/pdf", PDF_BYTES))
        self.assertEqual(stats["files_seen"], 7)          # LINK and BOX_COM are not FILEs
        self.assertEqual(stats["non_pdf_excluded"], 2)
        self.assertEqual(stats["pdf_metadata_conflicts"], 1)
        self.assertEqual(stats["pdf_content_verified"], 4)
        self.assertEqual(stats["uploads_confirmed"], 4)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(self.temp_files_left(), [])
        self.assert_only_allowed_calls({5000})

    def test_invalid_downloaded_signature_is_not_uploaded(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "error-page.pdf", content=HTML_BYTES)
        world.add_file(SRC, 1000, "renamed.pdf", mime="application/octet-stream",
                       content=b"PK\x03\x04xlsx")
        world.add_file(SRC, 1000, "good.pdf")
        syncer = self.make_syncer(world)
        stats = syncer.sync_attachments(apply=True)
        self.assertEqual([u[2] for u in world.uploads], ["good.pdf"])
        self.assertEqual(stats["pdf_signature_rejected"], 2)
        self.assertEqual(stats["pdf_content_verified"], 1)
        self.assertEqual(stats["errors"], 2)
        self.assertEqual(self.temp_files_left(), [])

    def test_resolved_metadata_conflict_at_download_time_skips(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "x.pdf", mime="application/pdf", detail_mime="text/html")
        syncer = self.make_syncer(world)
        stats = syncer.sync_attachments(apply=True)
        self.assertEqual(world.uploads, [])
        self.assertEqual(world.http_get_urls, [])
        self.assertEqual(stats["pdf_metadata_conflicts"], 1)

    def test_cached_and_uncached_paths_apply_identical_rules(self):
        results = []
        for use_cache in (True, False):
            with self.subTest(use_cache=use_cache):
                world = self.make_world([111], [111])
                add_mixed_row(world, 1000)
                syncer = self.make_syncer(world)
                cache = syncer._build_attachment_cache(SRC, [1000]) if use_cache else None
                uploaded = syncer.copy_attachments_to_row(
                    1000, 5000, apply=True, source_attachment_cache=cache
                )
                results.append((uploaded, [u[2] for u in world.uploads], dict(syncer.stats)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][1], MIXED_EXPECTED_UPLOADS)

    def test_failed_source_listing_in_cache_counts_one_error(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        world.list_failures[(SRC, 1000)] = [api_error()]
        syncer = self.make_syncer(world)
        stats = syncer.sync_attachments(apply=True)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(world.uploads, [])
        # target row never listed for a source row whose state is unknown
        listed = [c.args[:2] for c in self.read.Attachments.list_row_attachments.call_args_list]
        self.assertNotIn((TGT, 5000), listed)


# ------------------------------------------------------- duplicates / repeats


class DuplicatePolicyTests(SyncTestCase):
    def test_existing_target_filename_is_skipped(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        world.add_file(SRC, 1000, "b.pdf")
        world.add_file(TGT, 5000, "a.pdf")
        syncer = self.make_syncer(world)
        stats = syncer.sync_attachments(apply=True)
        self.assertEqual([u[2] for u in world.uploads], ["b.pdf"])
        self.assertEqual(stats["skipped_existing_filename"], 1)

    def test_repeated_source_filename_uploads_once(self):
        for apply in (False, True):
            with self.subTest(apply=apply):
                world = self.make_world([111], [111])
                world.add_file(SRC, 1000, "same.pdf")
                world.add_file(SRC, 1000, "same.pdf")
                syncer = self.make_syncer(world)
                stats = syncer.sync_attachments(apply=apply)
                self.assertEqual(stats["skipped_duplicate_source_filename"], 1)
                self.assertEqual(stats["pdf_planned"], 1)
                self.assertEqual(len(world.uploads), 1 if apply else 0)

    def test_repeat_run_adds_nothing(self):
        world = self.make_world([111, 222], [222, 111])
        world.add_file(SRC, 1000, "a.pdf")
        world.add_file(SRC, 1001, "b.pdf")
        first = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(first["uploads_confirmed"], 2)
        second = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(second["uploads_confirmed"], 0)
        self.assertEqual(second["skipped_existing_filename"], 2)
        self.assertEqual(len(world.uploads), 2)
        self.assertEqual(sorted((u[1], u[2]) for u in world.uploads), [(5000, "b.pdf"), (5001, "a.pdf")])

    def test_target_cache_set_is_updated_after_upload(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        syncer = self.make_syncer(world)
        target_cache = {5000: set()}
        syncer.copy_attachments_to_row(1000, 5000, apply=True, target_attachment_cache=target_cache)
        self.assertEqual(target_cache[5000], {"a.pdf"})
        again = syncer.copy_attachments_to_row(1000, 5000, apply=True, target_attachment_cache=target_cache)
        self.assertEqual(again, 0)
        self.assertEqual(len(world.uploads), 1)


# --------------------------------------------------------- target listing state


class TargetListingFailureTests(SyncTestCase):
    def test_failed_target_listing_prevents_uploads(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        world.list_failures[(TGT, 5000)] = [api_error()]
        syncer = self.make_syncer(world)
        stats = syncer.sync_attachments(apply=True)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(world.uploads, [])
        self.assertEqual(world.http_get_urls, [])

    def test_retryable_target_listing_failure_is_retried_then_skipped(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        world.list_failures[(TGT, 5000)] = [UnexpectedRequestError(None, None) for _ in range(3)]
        syncer = self.make_syncer(world)
        stats = syncer.sync_attachments(apply=True)
        target_lists = [c for c in self.read.Attachments.list_row_attachments.call_args_list
                        if c.args[:2] == (TGT, 5000)]
        self.assertEqual(len(target_lists), 3)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(world.uploads, [])

    def test_transient_target_listing_failure_recovers(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        world.list_failures[(TGT, 5000)] = [api_error(ServerTimeoutExceededError, 4002, "timeout")]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["uploads_confirmed"], 1)

    def test_incomplete_target_listing_is_not_treated_as_empty(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        world.incomplete_listing.add((TGT, 5000))
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(world.uploads, [])


# ------------------------------------------------------------- upload outcomes


class UploadOutcomeTests(SyncTestCase):
    def setup_row(self, names=("a.pdf",)):
        world = self.make_world([111], [111])
        for name in names:
            world.add_file(SRC, 1000, name)
        return world

    def target_listing_calls(self):
        return [c for c in self.read.Attachments.list_row_attachments.call_args_list
                if c.args[:2] == (TGT, 5000)]

    def test_ambiguous_upload_that_landed_is_confirmed_without_retry(self):
        world = self.setup_row()
        world.upload_effects = [(True, UnexpectedRequestError(None, None))]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(self.upload.Attachments.attach_file_to_row.call_count, 1)
        self.assertEqual(len(self.target_listing_calls()), 2)  # initial + reconciliation
        self.assertEqual(stats["uploads_confirmed"], 1)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(len(world.uploads), 1)

    def test_ambiguous_upload_not_listed_is_not_reposted_in_the_same_run(self):
        world = self.setup_row(("a.pdf", "b.pdf"))
        world.upload_effects = [(False, ServerTimeoutExceededError(NS(result=None), "timeout"))]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        sent = [c.args[2][0] for c in self.upload.Attachments.attach_file_to_row.call_args_list]
        self.assertEqual(sent, ["a.pdf", "b.pdf"])   # a.pdf POSTed once; b.pdf continues
        self.assertEqual(len(self.target_listing_calls()), 2)  # initial + reconciliation
        self.assertEqual(stats["uploads_unconfirmed"], 1)
        self.assertEqual(stats["uploads_confirmed"], 1)
        self.assertEqual(stats["errors"], 1)

        # Follow-up run: the fresh listing decides, and a.pdf is uploaded exactly once.
        second = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(second["uploads_confirmed"], 1)
        self.assertEqual(second["skipped_existing_filename"], 1)
        self.assertEqual(second["errors"], 0)
        self.assertEqual(sorted(u[2] for u in world.uploads), ["a.pdf", "b.pdf"])

    def test_same_named_file_is_not_posted_after_an_unconfirmed_copy(self):
        # The first copy may still be processing server-side, so its same-named twin
        # must not be POSTed to the same target row in this run.
        world = self.setup_row(("same.pdf", "same.pdf"))
        world.upload_effects = [(False, ServerTimeoutExceededError(NS(result=None), "timeout"))]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(self.upload.Attachments.attach_file_to_row.call_count, 1)
        self.assertEqual(stats["uploads_unconfirmed"], 1)
        self.assertEqual(stats["skipped_duplicate_source_filename"], 1)
        self.assertEqual(stats["errors"], 1)

    def test_same_named_file_is_not_posted_after_a_rejected_copy(self):
        world = self.setup_row(("same.pdf", "same.pdf"))
        world.upload_effects = [(False, api_error(code=1004, message="Not authorized"))]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(self.upload.Attachments.attach_file_to_row.call_count, 1)
        self.assertEqual(stats["skipped_duplicate_source_filename"], 1)
        self.assertEqual(stats["errors"], 1)

    def test_same_named_file_is_still_tried_when_the_first_never_reached_upload(self):
        # No POST happened for the first copy (its download failed), so nothing can be
        # duplicated and the twin gets its chance.
        world = self.setup_row(("same.pdf", "same.pdf"))
        world.download_failures = [403, 403, 403]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(self.upload.Attachments.attach_file_to_row.call_count, 1)
        self.assertEqual(stats["uploads_confirmed"], 1)
        self.assertEqual(stats["errors"], 1)

    def test_ambiguous_retryable_api_error_is_not_retried_blindly(self):
        world = self.setup_row()
        world.upload_effects = [(False, UnexpectedRequestError(None, None))]
        syncer = self.make_syncer(world, max_attempts=5)
        stats = syncer.sync_attachments(apply=True)
        self.assertEqual(self.upload.Attachments.attach_file_to_row.call_count, 1)
        self.assertEqual(stats["uploads_unconfirmed"], 1)

    def test_unresolvable_upload_is_reported_and_not_retried(self):
        world = self.setup_row(("a.pdf", "b.pdf"))
        world.upload_effects = [(False, requests.exceptions.ConnectionError("reset"))]
        syncer = self.make_syncer(world)
        # initial target listing succeeds, the reconciliation listing fails 3 times
        original = world.list_row_attachments
        calls = {"n": 0}

        def flaky(sheet_id, row_id, include_all=None):
            if (sheet_id, row_id) == (TGT, 5000):
                calls["n"] += 1
                if calls["n"] > 1:
                    raise UnexpectedRequestError(None, None)
            return original(sheet_id, row_id, include_all)

        self.read.Attachments.list_row_attachments.side_effect = flaky
        stats = syncer.sync_attachments(apply=True)
        self.assertEqual(self.upload.Attachments.attach_file_to_row.call_count, 1)
        self.assertEqual(stats["uploads_unresolved"], 1)
        self.assertEqual(stats["uploads_confirmed"], 0)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(self.temp_files_left(), [])

    def test_definite_rejection_is_not_retried(self):
        world = self.setup_row()
        world.upload_effects = [(False, api_error(code=1004, message="Not authorized"))]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(self.upload.Attachments.attach_file_to_row.call_count, 1)
        self.assertEqual(len(self.target_listing_calls()), 1)
        self.assertEqual(stats["errors"], 1)

    def test_rate_limit_waits_and_retries_without_reconciliation(self):
        world = self.setup_row()
        world.upload_effects = [(False, api_error(RateLimitExceededError, 4003, "Rate limit exceeded."))]
        stats = self.make_syncer(world, rate_limit_backoff=60.0).sync_attachments(apply=True)
        self.assertEqual(self.upload.Attachments.attach_file_to_row.call_count, 2)
        self.assertEqual(len(self.target_listing_calls()), 1)
        self.assertIn(60.0, self.clock.sleeps)
        self.assertEqual(stats["uploads_confirmed"], 1)
        self.assertEqual(stats["errors"], 0)

    def test_upload_posts_are_throttled(self):
        world = self.setup_row(("a.pdf", "b.pdf", "c.pdf"))
        self.make_syncer(world, upload_min_interval=2.5).sync_attachments(apply=True)
        starts = world.upload_starts
        self.assertEqual(len(starts), 3)
        for earlier, later in zip(starts, starts[1:]):
            self.assertGreaterEqual(later - earlier, 2.5)


# ----------------------------------------------------- downloads / dry run / logs


class DownloadAndDryRunTests(SyncTestCase):
    def test_dry_run_performs_no_downloads_uploads_or_mutations(self):
        world = self.make_world([111, 222], [111, 222])
        add_mixed_row(world, 1000)
        world.add_file(SRC, 1001, "c.pdf")
        world.add_file(TGT, 5001, "c.pdf")
        syncer = self.make_syncer(world)
        stats = syncer.sync_attachments(apply=False)
        self.upload.Attachments.attach_file_to_row.assert_not_called()
        self.requests_get.assert_not_called()
        self.assertFalse(os.path.exists(self.temp_folder))
        self.assertEqual(stats["pdf_planned"], 4)
        self.assertEqual(stats["pdf_content_verified"], 0)
        self.assertEqual(stats["uploads_confirmed"], 0)
        self.assertEqual(stats["skipped_existing_filename"], 1)
        self.assertLessEqual(self.sdk_calls(), ALLOWED_SDK_CALLS - {"Attachments.attach_file_to_row"})

    def test_download_retry_uses_a_fresh_url_each_attempt(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        world.download_failures = [requests.exceptions.ConnectionError("boom"), 503]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(len(world.http_get_urls), 3)
        self.assertEqual(len(set(world.http_get_urls)), 3)
        self.assertEqual(stats["uploads_confirmed"], 1)
        self.assertEqual(stats["errors"], 0)
        for call in self.requests_get.call_args_list:
            self.assertEqual(call.kwargs.get("timeout"), (10, 120))
            self.assertTrue(call.kwargs.get("stream"))

    def test_exhausted_download_counts_error_and_cleans_up(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        world.download_failures = [403, 403, 403]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(world.uploads, [])
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(self.temp_files_left(), [])

    def test_temp_file_removed_when_upload_raises_unexpectedly(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        syncer = self.make_syncer(world)
        with mock.patch.object(syncer, "_upload_pdf", side_effect=RuntimeError("bug")):
            stats = syncer.sync_attachments(apply=True)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(self.temp_files_left(), [])

    def test_signed_urls_never_logged(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        world.add_file(SRC, 1000, "b.pdf", content=HTML_BYTES)
        world.add_file(SRC, 1000, "c-no-mime", mime=None, detail_mime="application/pdf")
        world.download_failures = [403, requests.exceptions.ConnectionError(
            f"Max retries exceeded with url: /dl?{SIGNED_MARKER}")]
        with self.assertLogs(level="DEBUG") as logs:
            self.make_syncer(world).sync_attachments(apply=True)
        self.assertTrue(world.http_get_urls)
        for record in logs.records:
            self.assertNotIn(SIGNED_MARKER, record.getMessage())
            self.assertNotIn("signed.example.invalid", record.getMessage())
            self.assertNotIn(API_KEY, record.getMessage())


# ------------------------------------------------------------------ fix round


class TransportRetryTests(unittest.TestCase):
    """C1: the upload client must not re-send a POST at the urllib3 adapter level."""

    def test_real_sdk_client_adapters_stop_retrying_post(self):
        # Constructing a real client touches no network.
        client = smartsheet.Smartsheet("x" * 30, max_retry_time=0)
        adapters = client._session.adapters
        self.assertTrue(adapters)
        # The hazard exists before the fix: the SDK adapter retries a POST read error.
        before = [a.max_retries.increment("POST", error=urllib3.exceptions.ProtocolError("x"))
                  for a in adapters.values() if a.max_retries.total]
        self.assertTrue(before and all(isinstance(r, Retry) for r in before))

        disable_transport_retries(client)

        for prefix, adapter in adapters.items():
            with self.subTest(adapter=prefix):
                self.assertEqual(adapter.max_retries.total, 0)
                # A POST read error is re-raised, never turned into another attempt
                with self.assertRaises(urllib3.exceptions.ProtocolError):
                    adapter.max_retries.increment(
                        "POST", error=urllib3.exceptions.ProtocolError("x"))
                with self.assertRaises(urllib3.exceptions.MaxRetryError):
                    adapter.max_retries.increment(
                        "POST", error=urllib3.exceptions.NewConnectionError(None, "x"))

    def test_fails_closed_when_adapters_cannot_be_controlled(self):
        class StuckAdapter:
            @property
            def max_retries(self):
                return Retry(total=1)

            @max_retries.setter
            def max_retries(self, value):
                pass

        for client in (
            NS(),
            NS(_session=NS()),
            NS(_session=NS(adapters={})),
            NS(_session=NS(adapters=mock.MagicMock())),
            NS(_session=NS(adapters={"https://": StuckAdapter()})),
        ):
            with self.subTest(client=client):
                with self.assertRaises(RuntimeError):
                    disable_transport_retries(client)


class FixRoundTests(SyncTestCase):
    def test_upload_client_adapters_patched_and_reads_untouched(self):
        self.make_syncer(self.make_world([], []))
        read, upload = self.factory.created[0][0], self.factory.created[1][0]
        for adapter in upload._session.adapters.values():
            self.assertEqual(adapter.max_retries.total, 0)
        self.assertEqual(read._session.adapters["https://"].max_retries.total, 1)

    def test_syncer_refuses_to_start_without_controllable_adapters(self):
        self.factory.with_session = False
        with self.assertRaises(RuntimeError):
            AttachmentSyncer(API_KEY, SRC, TGT, SRC_COL, TGT_COL, temp_folder=self.temp_folder)

    # C3
    def test_listing_without_total_count_is_unknown(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        world.missing_total.add((TGT, 5000))
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(world.uploads, [])

    def test_every_listing_requests_all_pages(self):
        world = self.make_world([111, 222], [111, 222])
        world.add_file(SRC, 1000, "a.pdf")
        world.add_file(SRC, 1001, "b.pdf")
        world.upload_effects = [(True, UnexpectedRequestError(None, None))]  # adds a re-list
        self.make_syncer(world).sync_attachments(apply=True)
        calls = self.read.Attachments.list_row_attachments.call_args_list
        self.assertGreaterEqual(len(calls), 5)
        for call in calls:
            self.assertIs(call.kwargs.get("include_all"), True)

    # C4
    def test_only_row_and_discussion_files_are_eligible(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "row.pdf", parent_type="ROW")
        world.add_file(SRC, 1000, "discussion.pdf", parent_type="COMMENT")
        world.add_file(SRC, 1000, "sheet-level.pdf", parent_type="SHEET")
        world.add_file(SRC, 1000, "proof.pdf", parent_type=None)
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual([u[2] for u in world.uploads], ["row.pdf", "discussion.pdf"])
        self.assertEqual(stats["unsupported_parent_excluded"], 2)
        self.assertEqual(stats["errors"], 0)

    def test_real_sdk_proof_parent_is_excluded(self):
        from smartsheet.models import Attachment
        world = self.make_world([111], [111])
        proof = Attachment({"id": 1, "name": "proof.pdf", "attachmentType": "FILE",
                            "mimeType": "application/pdf", "parentType": "PROOF"})
        row = Attachment({"id": 2, "name": "row.pdf", "attachmentType": "FILE",
                          "mimeType": "application/pdf", "parentType": "ROW"})
        syncer = self.make_syncer(world)
        self.assertEqual(syncer._select_pdf_candidates(1000, [proof, row]), [row])

    # C6
    def test_filenames_that_cannot_round_trip_are_skipped(self):
        for name in ('quote".pdf', "line\nbreak.pdf", "cr\r.pdf", "del\x7f.pdf", "tab\t.pdf"):
            self.assertTrue(has_unsupported_filename_chars(name), name)
        self.assertFalse(has_unsupported_filename_chars("Plan rev 2 (final) é.pdf"))

        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, 'bad"name.pdf')
        world.add_file(SRC, 1000, "bad\nname.pdf")
        world.add_file(SRC, 1000, "good.pdf")
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual([u[2] for u in world.uploads], ["good.pdf"])
        self.assertEqual(stats["unsupported_filename_skipped"], 2)
        self.assertEqual(stats["errors"], 0)

    def test_stored_name_mismatch_is_reported_and_both_names_recorded(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        world.stored_names["a.pdf"] = "a (1).pdf"
        syncer = self.make_syncer(world)
        target_cache = {5000: set()}
        uploaded = syncer.copy_attachments_to_row(1000, 5000, apply=True,
                                                  target_attachment_cache=target_cache)
        self.assertEqual(uploaded, 1)
        self.assertEqual(syncer.stats["uploads_confirmed"], 1)
        self.assertEqual(syncer.stats["uploads_name_mismatch"], 1)
        self.assertEqual(syncer.stats["errors"], 1)
        self.assertEqual(target_cache[5000], {"a.pdf", "a (1).pdf"})

    # C8
    def test_download_falls_back_to_listing_mime(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "scan-no-extension", mime="application/pdf", detail_mime=None)
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual([u[2] for u in world.uploads], ["scan-no-extension"])
        self.assertEqual(stats["pdf_metadata_conflicts"], 0)

    def test_truncated_download_is_never_uploaded(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "big.pdf", content=PDF_BYTES * 50)
        world.download_failures = ["truncate", "truncate", "truncate"]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(world.uploads, [])
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(len(set(world.http_get_urls)), 3)
        self.assertEqual(self.temp_files_left(), [])

    def test_truncated_then_complete_download_uploads_full_file(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "big.pdf", content=PDF_BYTES * 50)
        world.download_failures = ["truncate"]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual([u[3] for u in world.uploads], [PDF_BYTES * 50])
        self.assertEqual(stats["errors"], 0)

    def test_encoded_response_skips_length_check(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        world.download_failures = ["gzip"]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(stats["uploads_confirmed"], 1)
        self.assertEqual(len(world.http_get_urls), 1)


if __name__ == "__main__":
    unittest.main()
