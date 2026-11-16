"""
Focused tests for the attachment sync (pdf_only and all_files routes). Offline:
Smartsheet clients, downloads and time are all mocked; no credentials, no
network, no real sleeping.
"""
import dataclasses
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
    upload_mime_type,
)
from src.config import FILE_RULE_ALL, FILE_RULE_PDF_ONLY, VERIFIED_ROUTES, Route

SRC = 1553121697288068
TGT = 573385022328708            # Project List - Blank Copy (pdf_only)
SRC_COL = 8022925527175044
TGT_COL = 7068369005875076
MASTER = 4850043816202116        # master schedule (all_files)
MASTER_COL = 1437881091182468
OTHER_COL = 111

WR_TITLE = "Work Request #"
BLANK_ROUTE = Route("blank-copy", SRC, TGT, SRC_COL, TGT_COL, FILE_RULE_PDF_ONLY, WR_TITLE, WR_TITLE)
MASTER_ROUTE = Route("master-schedule", SRC, MASTER, SRC_COL, MASTER_COL, FILE_RULE_ALL, WR_TITLE, "WR")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSX_BYTES = b"PK\x03\x04 xlsx body"
PNG_BYTES = b"\x89PNG\r\n\x1a\n image body"
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

    def __init__(self, clock, source_values, target_values, source_title=None,
                 target_title=None, route=BLANK_ROUTE):
        self.clock = clock
        self.route = route
        self.target = route.target_sheet_id
        # Live titles default to the route's pinned titles
        source_title = route.source_match_column_title if source_title is None else source_title
        target_title = route.target_match_column_title if target_title is None else target_title
        self.sheets = {
            SRC: make_sheet(SRC, SRC_COL, source_values, 1000, source_title),
            self.target: make_sheet(self.target, route.target_match_column_id, target_values,
                                    5000, target_title),
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
        # Sheet paging: sheet_id -> rows served by an un-paged first request (None = all)
        self.served_page_size = {}
        self.page_failures = {}   # (sheet_id, page) -> exception
        self.page_rows = {}       # (sheet_id, page) -> rows override for that page
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
    def get_sheet(self, sheet_id, page_size=None, page=None):
        sheet = self.sheets[sheet_id]
        served = self.served_page_size.get(sheet_id)
        if page is None and served is None:
            return sheet                      # whole sheet in one response (today's behaviour)
        if page is None:
            page, page_size = 1, served       # un-paged request, server default page size
        if (sheet_id, page) in self.page_failures:
            raise self.page_failures[(sheet_id, page)]
        rows = self.page_rows.get((sheet_id, page))
        if rows is None:
            start = (page - 1) * page_size
            rows = sheet.rows[start:start + page_size]
        return NS(id=sheet.id, columns=sheet.columns, rows=list(rows),
                  total_row_count=sheet.total_row_count)

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
        syncer = AttachmentSyncer(API_KEY, world.route, temp_folder=self.temp_folder, **kwargs)
        self.target = world.target
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
            self.assertEqual(sheet_id, self.target)
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
        bad_routes = [
            dataclasses.replace(BLANK_ROUTE, file_rule=FILE_RULE_ALL),        # Blank Copy + all files
            dataclasses.replace(MASTER_ROUTE, file_rule=FILE_RULE_PDF_ONLY),  # master + pdf only
            dataclasses.replace(BLANK_ROUTE, target_match_column_id=MASTER_COL),
            dataclasses.replace(MASTER_ROUTE, target_match_column_id=TGT_COL),
            dataclasses.replace(BLANK_ROUTE, target_sheet_id=SRC),            # same-sheet routing
            Route("swapped", TGT, SRC, TGT_COL, SRC_COL, FILE_RULE_PDF_ONLY, WR_TITLE, WR_TITLE),
            dataclasses.replace(MASTER_ROUTE, target_match_column_title=WR_TITLE),  # wrong title
            dataclasses.replace(BLANK_ROUTE, target_match_column_title="WR"),
            dataclasses.replace(BLANK_ROUTE, source_match_column_title="WR"),
            dataclasses.replace(BLANK_ROUTE, target_sheet_id=999),            # unknown target
            dataclasses.replace(BLANK_ROUTE, target_sheet_id="not-a-number"),
            dataclasses.replace(BLANK_ROUTE, target_sheet_id=str(TGT)),       # non-integer ID
            dataclasses.replace(BLANK_ROUTE, name="other"),
            (SRC, TGT, SRC_COL, TGT_COL),                                     # not a Route
        ]
        for route in bad_routes:
            with self.subTest(route=route):
                with self.assertRaises(RoutingError):
                    AttachmentSyncer(API_KEY, route, temp_folder=self.temp_folder)
        self.assertEqual(self.factory.created, [])

    def test_unknown_file_rule_fails_closed_before_clients_exist(self):
        from src import config
        for rule in ("pdf-only", None):
            typo = dataclasses.replace(BLANK_ROUTE, file_rule=rule)
            with self.subTest(rule=rule, layer="validate_route"):
                with mock.patch.object(config, "VERIFIED_ROUTES", (MASTER_ROUTE, typo)):
                    with self.assertRaises(RoutingError) as ctx:
                        AttachmentSyncer(API_KEY, typo, temp_folder=self.temp_folder)
                self.assertIn("unknown file rule", str(ctx.exception))
            with self.subTest(rule=rule, layer="explicit syncer check"):
                # Even if route validation were bypassed, the syncer refuses the rule.
                with mock.patch.object(attachment_sync, "validate_route", return_value=[]):
                    with self.assertRaises(RoutingError) as ctx:
                        AttachmentSyncer(API_KEY, typo, temp_folder=self.temp_folder)
                self.assertIn("unknown file rule", str(ctx.exception))
        self.assertEqual(self.factory.created, [])

    def test_each_verified_route_constructs(self):
        for route in VERIFIED_ROUTES:
            with self.subTest(route=route.name):
                syncer = AttachmentSyncer(API_KEY, route, temp_folder=self.temp_folder)
                self.assertEqual(
                    (syncer.source_sheet_id, syncer.target_sheet_id,
                     syncer.source_match_column_id, syncer.target_match_column_id),
                    (route.source_sheet_id, route.target_sheet_id,
                     route.source_match_column_id, route.target_match_column_id),
                )
        self.assertEqual(len(self.factory.created), 4)  # read + upload client per route

    # BUG D: the temp folder never comes from the environment
    def test_default_temp_folder_is_the_fixed_config_path(self):
        fixed = os.path.join(tempfile.gettempdir(), "smartsheet-attachments")
        with mock.patch.dict(os.environ, {"TEMP_DOWNLOAD_FOLDER": os.path.join(self.temp_folder, "env")}):
            syncer = AttachmentSyncer(API_KEY, BLANK_ROUTE)
        self.assertEqual(syncer.temp_folder, fixed)

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
        world.sheets[TGT].id = MASTER
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

    # BUG A: an unknown total row count must fail closed, like attachment listings
    def test_missing_total_row_count_fails_closed(self):
        for side, sheet_id in (("source", SRC), ("target", TGT)):
            for total in (None, "missing", True, "1", 1.0):
                with self.subTest(side=side, total=total):
                    world = self.make_world(["1"], ["1"])
                    world.add_file(SRC, 1000, "a.pdf")
                    if total == "missing":
                        del world.sheets[sheet_id].total_row_count
                    else:
                        world.sheets[sheet_id].total_row_count = total
                    syncer = self.make_syncer(world)
                    with self.assertRaises(RoutingError) as ctx:
                        syncer.sync_attachments(apply=True)
                    message = str(ctx.exception)
                    for part in ("blank-copy", side, str(sheet_id), "total row count"):
                        self.assertIn(part, message)
                    self.read.Attachments.list_row_attachments.assert_not_called()
                    self.upload.Attachments.attach_file_to_row.assert_not_called()

    def test_integer_total_matching_rows_still_passes(self):
        for source, target in (([], []), (["1"], ["1"]), ([111, 222], [222])):
            with self.subTest(source=source, target=target):
                stats = self.make_syncer(self.make_world(source, target)).sync_attachments(apply=False)
                self.assertEqual(stats["errors"], 0)

    def test_blank_copy_requires_work_request_title_on_both_sides(self):
        for kwargs in ({"target_title": "WR"}, {"source_title": "WR"}):
            with self.subTest(**kwargs):
                self.run_expecting_routing_error(self.make_world(["1"], ["1"], **kwargs))

    def test_master_schedule_preflight_passes_with_wr_title(self):
        world = self.make_world([111], [111], route=MASTER_ROUTE)
        self.assertEqual(world.sheets[MASTER].columns[1].title, "WR")
        world.add_file(SRC, 1000, "a.xlsx", mime=XLSX_MIME)
        with self.assertLogs("src.attachment_sync", level="INFO") as logs:
            stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["uploads_confirmed"], 1)
        text = "\n".join(logs.output)
        self.assertIn(f"Target preflight OK: sheet {MASTER}, column {MASTER_COL} 'WR'", text)
        self.assertIn(f"Source preflight OK: sheet {SRC}, column {SRC_COL} 'Work Request #'", text)

    def test_master_schedule_titles_are_not_interchangeable(self):
        cases = (
            ("target", {"target_title": "Work Request #"}, MASTER_COL, "Work Request #", "WR"),
            ("source", {"source_title": "WR"}, SRC_COL, "WR", "Work Request #"),
        )
        for side, kwargs, column, actual, expected in cases:
            with self.subTest(side=side):
                world = self.make_world([111], [111], route=MASTER_ROUTE, **kwargs)
                syncer = self.make_syncer(world)
                with self.assertRaises(RoutingError) as ctx:
                    syncer.sync_attachments(apply=True)
                message = str(ctx.exception)
                for part in ("master-schedule", str(column), repr(actual), repr(expected)):
                    self.assertIn(part, message)
                self.upload.Attachments.attach_file_to_row.assert_not_called()
                self.read.Attachments.list_row_attachments.assert_not_called()


# --------------------------------------------------------------- sheet paging


class SheetPagingTests(SyncTestCase):
    """BUG B: a first get_sheet response with fewer rows than total_row_count."""

    def paged_world(self, n=250, served=100):
        values = list(range(1, n + 1))
        world = self.make_world(values, values)
        world.served_page_size[SRC] = served
        return world

    def sheet_calls(self, sheet_id):
        return [c for c in self.read.Sheets.get_sheet.call_args_list if c.args[0] == sheet_id]

    def run_expecting_routing_error(self, world):
        syncer = self.make_syncer(world)
        with self.assertRaises(RoutingError) as ctx:
            syncer.sync_attachments(apply=True)
        self.read.Attachments.list_row_attachments.assert_not_called()
        self.upload.Attachments.attach_file_to_row.assert_not_called()
        return str(ctx.exception)

    def test_remaining_pages_are_fetched_and_combined_in_order(self):
        world = self.paged_world()
        world.add_file(SRC, 1249, "last-page.pdf")   # row 250, served on page 3
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(self.sheet_calls(SRC), [
            mock.call(SRC),
            mock.call(SRC, page_size=100, page=2),
            mock.call(SRC, page_size=100, page=3),
        ])
        self.assertEqual(self.sheet_calls(TGT), [mock.call(TGT)])
        self.assertEqual(stats["source_rows_total"], 250)
        self.assertEqual(stats["wr_matched"], 250)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual([(u[1], u[2]) for u in world.uploads], [(5249, "last-page.pdf")])

    def test_complete_first_response_makes_exactly_one_call_per_sheet(self):
        world = self.make_world([111, 222], [111, 222])
        self.make_syncer(world).sync_attachments(apply=False)
        self.assertEqual(self.read.Sheets.get_sheet.call_args_list, [mock.call(SRC), mock.call(TGT)])

    def test_failed_later_page_fails_the_route_without_writes(self):
        world = self.paged_world()
        world.add_file(SRC, 1000, "a.pdf")
        world.page_failures[(SRC, 3)] = api_error()
        syncer = self.make_syncer(world)
        with self.assertRaises(ApiError):
            syncer.sync_attachments(apply=True)
        self.assertGreaterEqual(syncer.stats["errors"], 1)
        self.read.Attachments.list_row_attachments.assert_not_called()
        self.upload.Attachments.attach_file_to_row.assert_not_called()

    def test_overlapping_pages_fail_closed(self):
        world = self.paged_world()
        world.page_rows[(SRC, 2)] = world.sheets[SRC].rows[50:150]   # 250 rows, 50 repeated
        self.assertIn("duplicate row", self.run_expecting_routing_error(world))

    def test_combined_count_mismatch_fails_closed(self):
        world = self.paged_world()
        world.page_rows[(SRC, 3)] = world.sheets[SRC].rows[200:240]  # rows removed meanwhile
        self.assertIn("240 of 250", self.run_expecting_routing_error(world))

    def test_empty_later_page_stops_paging_and_fails_closed(self):
        world = self.paged_world()
        world.page_rows[(SRC, 2)] = []
        self.assertIn("100 of 250", self.run_expecting_routing_error(world))
        self.assertEqual(len(self.sheet_calls(SRC)), 2)   # page 3 never requested

    def test_zero_rows_with_positive_total_fails_closed_without_paging(self):
        world = self.make_world([111], [111])
        world.sheets[SRC].rows = []
        self.assertIn("0 of 1", self.run_expecting_routing_error(world))
        self.assertEqual(self.sheet_calls(SRC), [mock.call(SRC)])


# ------------------------------------------------------------------- matching


class MatchingTests(SyncTestCase):
    def test_numeric_and_text_keys_match(self):
        world = self.make_world([92873293.0], [" 92873293 "])
        world.add_file(SRC, 1000, "a.pdf")
        syncer = self.make_syncer(world)
        stats = syncer.sync_attachments(apply=False)
        self.assertEqual(stats["wr_matched"], 1)
        self.assertEqual(stats["files_planned"], 1)

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

    # BUG C: a WR duplicated only in the target sheet must be reported
    def test_target_only_duplicate_wr_is_reported(self):
        world = self.make_world([111], [111, 222, 222, "333", 333.0])
        world.add_file(SRC, 1000, "a.pdf")
        syncer = self.make_syncer(world)
        with self.assertLogs("src.attachment_sync", level="INFO") as logs:
            stats = syncer.sync_attachments(apply=True)
        self.assertEqual(stats["wr_target_only_duplicates"], 2)
        self.assertEqual((stats["wr_matched"], stats["wr_unmatched"], stats["wr_ambiguous"]), (1, 0, 0))
        self.assertEqual([u[1] for u in world.uploads], [5000])
        warnings = [r.getMessage() for r in logs.records if r.levelno == logging.WARNING]
        self.assertTrue(any("WR 222" in w and "[5001, 5002]" in w for w in warnings), warnings)
        self.assertTrue(any("WR 333" in w and "[5003, 5004]" in w for w in warnings), warnings)
        self.assertIn("[blank-copy] WRs duplicated in target only (no source row): 2",
                      "\n".join(r.getMessage() for r in logs.records))

    def test_duplicate_also_in_source_is_counted_as_ambiguous_only(self):
        # 111 duplicated in target with a source row; 222 duplicated in source only
        world = self.make_world([111, 222, 222, 444], [111, 111, 222, 333])
        stats = self.make_syncer(world).sync_attachments(apply=False)
        self.assertEqual(stats["wr_ambiguous"], 2)
        self.assertEqual(stats["wr_target_only_duplicates"], 0)
        self.assertEqual((stats["wr_matched"], stats["wr_unmatched"]), (0, 1))

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
                self.assertEqual(stats["files_planned"], 1)
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
        self.assertEqual(stats["files_planned"], 4)
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
        with mock.patch.object(syncer, "_upload_file", side_effect=RuntimeError("bug")):
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
            AttachmentSyncer(API_KEY, BLANK_ROUTE, temp_folder=self.temp_folder)

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
        self.assertEqual(syncer._select_candidates(1000, [proof, row]), [row])

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


# ------------------------------------------------------ all_files (master schedule)


class UploadMimeTypeTests(unittest.TestCase):
    def test_first_well_formed_candidate_wins(self):
        self.assertEqual(upload_mime_type(XLSX_MIME, "image/png"), XLSX_MIME)
        self.assertEqual(upload_mime_type(None, "image/png"), "image/png")
        self.assertEqual(upload_mime_type("", " Text/Plain; charset=utf-8 "), "text/plain")
        self.assertEqual(upload_mime_type(None, None), "application/octet-stream")
        self.assertEqual(upload_mime_type(), "application/octet-stream")

    def test_generic_values_are_skipped_until_the_fallback(self):
        self.assertEqual(upload_mime_type("application/octet-stream", "image/png"), "image/png")
        self.assertEqual(upload_mime_type("Binary/Octet-Stream", XLSX_MIME), XLSX_MIME)
        self.assertEqual(upload_mime_type("binary/octet-stream", "application/octet-stream"),
                         "application/octet-stream")

    def test_malformed_values_are_never_sent(self):
        for bad in ("text/html\r\nX-Injected: 1", 'image/"png"', "no-slash", "a b/c", "/x"):
            with self.subTest(bad=bad):
                self.assertEqual(upload_mime_type(bad), "application/octet-stream")


class AllFilesRouteTests(SyncTestCase):
    def make_world(self, source_values, target_values, **kwargs):
        kwargs.setdefault("route", MASTER_ROUTE)
        return super().make_world(source_values, target_values, **kwargs)

    def add_mixed_files(self, world, row_id=1000):
        world.add_file(SRC, row_id, "costs.xlsx", mime=XLSX_MIME, content=XLSX_BYTES)
        world.add_file(SRC, row_id, "photo.png", mime="image/png", content=PNG_BYTES)
        world.add_file(SRC, row_id, "permit.pdf")
        world.add_file(SRC, row_id, "Drawing link", mime=None, attachment_type="LINK")

    def test_spreadsheet_image_and_pdf_planned_in_dry_run(self):
        world = self.make_world([111], [111])
        self.add_mixed_files(world)
        stats = self.make_syncer(world).sync_attachments(apply=False)
        self.assertEqual(stats["files_planned"], 3)
        self.assertEqual(stats["files_seen"], 3)   # the LINK is not a FILE
        self.upload.Attachments.attach_file_to_row.assert_not_called()
        self.requests_get.assert_not_called()
        self.assertFalse(os.path.exists(self.temp_folder))
        self.assertLessEqual(self.sdk_calls(), ALLOWED_SDK_CALLS - {"Attachments.attach_file_to_row"})

    def test_spreadsheet_image_and_pdf_uploaded_with_source_mime(self):
        world = self.make_world([111], [111])
        self.add_mixed_files(world)
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(
            [(u[0], u[1], u[2], u[3], u[4]) for u in world.uploads],
            [
                (MASTER, 5000, "costs.xlsx", XLSX_BYTES, XLSX_MIME),
                (MASTER, 5000, "photo.png", PNG_BYTES, "image/png"),
                (MASTER, 5000, "permit.pdf", PDF_BYTES, "application/pdf"),
            ],
        )
        self.assertEqual(stats["uploads_confirmed"], 3)
        self.assertEqual(stats["errors"], 0)
        for key in ("non_pdf_excluded", "pdf_metadata_conflicts", "pdf_signature_rejected",
                    "pdf_content_verified"):
            self.assertEqual(stats[key], 0, key)
        self.assertEqual(self.temp_files_left(), [])
        self.assert_only_allowed_calls({5000})

    def test_resolved_mime_is_preferred_over_listing_mime(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "scan.png", mime="image/png", detail_mime="image/jpeg",
                       content=PNG_BYTES)
        self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual([u[4] for u in world.uploads], ["image/jpeg"])

    def test_upload_mime_order_skips_generic_and_malformed_values(self):
        cases = [
            # (listing MIME, resolved MIME, uploaded MIME)
            ("application/octet-stream", "image/png", "image/png"),     # generic listing
            ("binary/octet-stream", XLSX_MIME, XLSX_MIME),
            ("image/png", "application/octet-stream", "image/png"),     # generic resolved
            ("image/png", None, "image/png"),                           # missing resolved
            ("image/png", "image/jpeg", "image/jpeg"),                  # both specific
            ("binary/octet-stream", "application/octet-stream", "application/octet-stream"),
            ("application/octet-stream", None, "application/octet-stream"),
            (None, None, "application/octet-stream"),
            ("image/png", "text/html\r\nX-Injected: 1", "image/png"),   # malformed resolved
        ]
        for listing, resolved, expected in cases:
            with self.subTest(listing=listing, resolved=resolved):
                world = self.make_world([111], [111])
                world.add_file(SRC, 1000, "file", mime=listing, detail_mime=resolved,
                               content=b"bytes")
                stats = self.make_syncer(world).sync_attachments(apply=True)
                self.assertEqual([u[4] for u in world.uploads], [expected])
                self.assertEqual(stats["errors"], 0)
                # No extra API call for the MIME choice: one get_attachment at download,
                # plus one at selection only when the listing has no MIME type.
                self.assertEqual(self.read.Attachments.get_attachment.call_count,
                                 1 if listing else 2)

    def test_missing_mime_is_resolved_via_get_attachment(self):
        world = self.make_world([111], [111])
        att = world.add_file(SRC, 1000, "scan", mime=None, detail_mime="image/png", content=PNG_BYTES)
        syncer = self.make_syncer(world)
        syncer.sync_attachments(apply=False)
        self.read.Attachments.get_attachment.assert_called_with(SRC, att.id)
        self.assertEqual(syncer.stats["files_planned"], 1)
        self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual([(u[2], u[4]) for u in world.uploads], [("scan", "image/png")])

    def test_unresolvable_but_readable_mime_falls_back_to_octet_stream(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "data.bin", mime=None, detail_mime=None, content=b"\x00\x01")
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual([(u[2], u[4]) for u in world.uploads],
                         [("data.bin", "application/octet-stream")])
        self.assertEqual(stats["errors"], 0)

    def test_failed_metadata_read_counts_error_and_skips_that_file(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "unknown", mime=None, content=b"x")
        world.add_file(SRC, 1000, "costs.xlsx", mime=XLSX_MIME, content=XLSX_BYTES)
        syncer = self.make_syncer(world)
        self.read.Attachments.get_attachment.side_effect = api_error()
        stats = syncer.sync_attachments(apply=False)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["files_planned"], 1)

    def test_pdf_name_without_signature_is_uploaded(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "error-page.pdf", content=HTML_BYTES)
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual([(u[2], u[3]) for u in world.uploads], [("error-page.pdf", HTML_BYTES)])
        self.assertEqual(stats["pdf_signature_rejected"], 0)
        self.assertEqual(stats["errors"], 0)

    def test_pdf_extension_with_non_pdf_mime_is_uploaded(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "fake.pdf", mime="image/png", content=PNG_BYTES)
        world.add_file(SRC, 1000, "later.pdf", mime="application/pdf", detail_mime="text/html")
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual([(u[2], u[4]) for u in world.uploads],
                         [("fake.pdf", "image/png"), ("later.pdf", "text/html")])
        self.assertEqual(stats["pdf_metadata_conflicts"], 0)
        self.assertEqual(stats["errors"], 0)

    def test_temp_file_name_never_comes_from_the_attachment_name(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "../../evil.xlsx", mime=XLSX_MIME, content=XLSX_BYTES)
        real_mkstemp = tempfile.mkstemp
        with mock.patch.object(attachment_sync.tempfile, "mkstemp", side_effect=real_mkstemp) as spy:
            self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(spy.call_count, 1)
        kwargs = spy.call_args.kwargs
        self.assertEqual((kwargs["prefix"], kwargs["suffix"], kwargs["dir"]),
                         ("wr-file-", ".tmp", self.temp_folder))
        self.assertEqual([u[2] for u in world.uploads], ["../../evil.xlsx"])
        self.assertEqual(self.temp_files_left(), [])

    def test_log_lines_name_the_route_and_never_say_pdf(self):
        world = self.make_world([111], [111])
        self.add_mixed_files(world)
        with self.assertLogs("src.attachment_sync", level="DEBUG") as logs:
            self.make_syncer(world).sync_attachments(apply=True)
        for record in logs.records:
            message = record.getMessage()
            self.assertTrue(message.startswith("[master-schedule] "), message)
            self.assertNotIn("PDF", message)

    # --- shared rules still enforced on the all_files route ---------------

    def test_parent_type_allowlist(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "row.xlsx", mime=XLSX_MIME, parent_type="ROW")
        world.add_file(SRC, 1000, "discussion.png", mime="image/png", parent_type="COMMENT")
        world.add_file(SRC, 1000, "sheet-level.xlsx", mime=XLSX_MIME, parent_type="SHEET")
        world.add_file(SRC, 1000, "proof.png", mime="image/png", parent_type=None)
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual([u[2] for u in world.uploads], ["row.xlsx", "discussion.png"])
        self.assertEqual(stats["unsupported_parent_excluded"], 2)

    def test_filename_character_skip(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, 'bad"name.xlsx', mime=XLSX_MIME)
        world.add_file(SRC, 1000, "bad\nname.png", mime="image/png")
        world.add_file(SRC, 1000, "good.xlsx", mime=XLSX_MIME)
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual([u[2] for u in world.uploads], ["good.xlsx"])
        self.assertEqual(stats["unsupported_filename_skipped"], 2)
        self.assertEqual(stats["errors"], 0)

    def test_existing_target_filename_is_skipped(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "costs.xlsx", mime=XLSX_MIME)
        world.add_file(SRC, 1000, "photo.png", mime="image/png")
        world.add_file(MASTER, 5000, "costs.xlsx", mime=XLSX_MIME)
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual([u[2] for u in world.uploads], ["photo.png"])
        self.assertEqual(stats["skipped_existing_filename"], 1)

    def test_same_named_twin_is_sent_once(self):
        for apply in (False, True):
            with self.subTest(apply=apply):
                world = self.make_world([111], [111])
                world.add_file(SRC, 1000, "same.xlsx", mime=XLSX_MIME)
                world.add_file(SRC, 1000, "same.xlsx", mime=XLSX_MIME)
                stats = self.make_syncer(world).sync_attachments(apply=apply)
                self.assertEqual(stats["skipped_duplicate_source_filename"], 1)
                self.assertEqual(stats["files_planned"], 1)
                self.assertEqual(len(world.uploads), 1 if apply else 0)

    def test_post_attempted_blocks_twin_after_rejected_upload(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "same.png", mime="image/png")
        world.add_file(SRC, 1000, "same.png", mime="image/png")
        world.upload_effects = [(False, api_error(code=1004, message="Not authorized"))]
        syncer = self.make_syncer(world)
        stats = syncer.sync_attachments(apply=True)
        self.assertEqual(self.upload.Attachments.attach_file_to_row.call_count, 1)
        self.assertIn((5000, "same.png"), syncer._post_attempted)
        self.assertEqual(stats["skipped_duplicate_source_filename"], 1)
        self.assertEqual(stats["errors"], 1)

    def test_content_length_mismatch_is_an_error_and_nothing_uploaded(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "big.xlsx", mime=XLSX_MIME, content=XLSX_BYTES * 50)
        world.download_failures = ["truncate", "truncate", "truncate"]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(world.uploads, [])
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(self.temp_files_left(), [])

    def test_unknown_target_listing_means_no_writes(self):
        for failure in ("error", "incomplete", "no-total"):
            with self.subTest(failure=failure):
                world = self.make_world([111], [111])
                world.add_file(SRC, 1000, "costs.xlsx", mime=XLSX_MIME)
                if failure == "error":
                    world.list_failures[(MASTER, 5000)] = [api_error()]
                elif failure == "incomplete":
                    world.incomplete_listing.add((MASTER, 5000))
                else:
                    world.missing_total.add((MASTER, 5000))
                stats = self.make_syncer(world).sync_attachments(apply=True)
                self.assertEqual(stats["errors"], 1)
                self.assertEqual(world.uploads, [])
                self.upload.Attachments.attach_file_to_row.assert_not_called()

    def test_ambiguous_upload_is_relisted_once_and_not_resent(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "costs.xlsx", mime=XLSX_MIME)
        world.add_file(SRC, 1000, "photo.png", mime="image/png")
        world.upload_effects = [(False, ServerTimeoutExceededError(NS(result=None), "timeout"))]
        stats = self.make_syncer(world, max_attempts=5).sync_attachments(apply=True)
        sent = [c.args[2][0] for c in self.upload.Attachments.attach_file_to_row.call_args_list]
        self.assertEqual(sent, ["costs.xlsx", "photo.png"])
        target_lists = [c for c in self.read.Attachments.list_row_attachments.call_args_list
                        if c.args[:2] == (MASTER, 5000)]
        self.assertEqual(len(target_lists), 2)  # initial + one reconciliation
        self.assertEqual(stats["uploads_unconfirmed"], 1)
        self.assertEqual(stats["errors"], 1)

    def test_ambiguous_upload_that_landed_is_confirmed(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "costs.xlsx", mime=XLSX_MIME)
        world.upload_effects = [(True, UnexpectedRequestError(None, None))]
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(self.upload.Attachments.attach_file_to_row.call_count, 1)
        self.assertEqual(stats["uploads_confirmed"], 1)
        self.assertEqual(stats["errors"], 0)

    def test_ambiguous_wr_skipped_on_master_schedule(self):
        world = self.make_world([111, 222], [111, 111, 222])
        world.add_file(SRC, 1000, "a.xlsx", mime=XLSX_MIME)
        world.add_file(SRC, 1001, "b.xlsx", mime=XLSX_MIME)
        stats = self.make_syncer(world).sync_attachments(apply=True)
        self.assertEqual(stats["wr_ambiguous"], 1)
        self.assertEqual([(u[1], u[2]) for u in world.uploads], [(5002, "b.xlsx")])
        self.assert_only_allowed_calls({5002})

    def test_signed_urls_never_logged(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "costs.xlsx", mime=XLSX_MIME)
        world.add_file(SRC, 1000, "no-mime", mime=None, detail_mime="image/png")
        world.download_failures = [403, requests.exceptions.ConnectionError(
            f"Max retries exceeded with url: /dl?{SIGNED_MARKER}")]
        with self.assertLogs(level="DEBUG") as logs:
            self.make_syncer(world).sync_attachments(apply=True)
        for record in logs.records:
            self.assertNotIn(SIGNED_MARKER, record.getMessage())
            self.assertNotIn(API_KEY, record.getMessage())


class PdfOnlyRouteLabelTests(SyncTestCase):
    def test_pdf_route_logs_name_the_route(self):
        world = self.make_world([111], [111])
        world.add_file(SRC, 1000, "a.pdf")
        with self.assertLogs("src.attachment_sync", level="INFO") as logs:
            self.make_syncer(world).sync_attachments(apply=False)
        self.assertTrue(all(r.getMessage().startswith("[blank-copy] ") for r in logs.records))
        text = "\n".join(r.getMessage() for r in logs.records)
        self.assertIn("PDFs planned (new filenames): 1", text)
        self.assertIn("PDFs content-verified", text)


if __name__ == "__main__":
    unittest.main()
