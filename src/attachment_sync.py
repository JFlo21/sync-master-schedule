"""
Core attachment synchronization logic for Smartsheet.

Attachment-only sync for one pinned route (see src/config.py VERIFIED_ROUTES):
reads the source sheet (Project List), matches existing rows in the route's
target sheet by Work Request #, and copies FILE attachments to the matching
target rows. The route's file rule decides which files: all files
(Master Schedule) or PDF files only (Project List - Blank Copy). It never
creates, updates, moves or deletes rows or cells; the only Smartsheet mutation
is attaching a file to an existing, verified target row.
"""
import logging
import math
import os
import re
import tempfile
import time
from collections.abc import Mapping, Sequence
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests
import smartsheet
from smartsheet.exceptions import (
    ApiError,
    HttpError,
    RateLimitExceededError,
    UnexpectedRequestError,
)
from urllib3.util.retry import Retry

from .config import FILE_RULE_PDF_ONLY, KNOWN_FILE_RULES, Route, validate_route

logger = logging.getLogger(__name__)

PDF_MIME_TYPE = "application/pdf"
FALLBACK_MIME_TYPE = "application/octet-stream"
_GENERIC_MIME_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})
# type/subtype made of RFC 6838 restricted-name characters (no quotes, spaces or controls)
_MIME_TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*")

# Integer-like Work Request # text: ASCII digits, no sign/exponent/separators,
# optional ".0..." suffix (Smartsheet stores numbers as floats, e.g. "92873293.0").
_WR_TEXT_PATTERN = re.compile(r"[1-9][0-9]*(?:\.0+)?")
_MAX_EXACT_FLOAT_INT = 2 ** 53

_PDF_SIGNATURE = b"%PDF-"
_PDF_SIGNATURE_WINDOW = 1024

# Parents whose FILE attachments belong to the row (row itself, row discussions).
# Sheet-level files and proofs (which deserialize to an empty parent) are excluded.
_ALLOWED_PARENT_TYPES = ("ROW", "COMMENT")

# Transport-level failures (the SDK wraps requests errors in these)
_TRANSPORT_ERRORS = (UnexpectedRequestError, HttpError, requests.exceptions.RequestException)


class RoutingError(Exception):
    """Source/target routing or match-column preflight failed; nothing was written."""


class AttachmentListingError(Exception):
    """An attachment listing could not be retrieved completely; the row state is unknown."""


class TargetStateUnknownError(Exception):
    """An upload outcome could not be reconciled; stop writing to that target row."""


def normalize_work_request(value: Any) -> Tuple[Optional[int], str]:
    """
    Normalize a Work Request # cell value to an exact integer key.

    Returns (key, status) where status is "valid", "blank" or "invalid".
    Accepts 92873293, 92873293.0, "92873293" and " 92873293.0 " as the same key.
    Never parses through float(str): text must be integer-like as written.
    """
    if value is None:
        return None, "blank"
    if isinstance(value, bool):  # bool is an int subclass; check it first
        return None, "invalid"
    if isinstance(value, int):
        return (value, "valid") if value > 0 else (None, "invalid")
    if isinstance(value, float):
        if (
            math.isfinite(value)
            and value.is_integer()
            and 0 < value <= _MAX_EXACT_FLOAT_INT
        ):
            return int(value), "valid"
        return None, "invalid"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None, "blank"
        if _WR_TEXT_PATTERN.fullmatch(text):
            return int(text.split(".", 1)[0]), "valid"
        return None, "invalid"
    return None, "invalid"


def _normalize_mime(mime_type: Any) -> str:
    """Lower-case MIME type without parameters; "" when absent."""
    if mime_type is None:
        return ""
    return str(mime_type).strip().lower().split(";", 1)[0].strip()


def classify_pdf_metadata(name: Any, mime_type: Any) -> Tuple[str, str]:
    """
    Decide from metadata whether an attachment is a PDF candidate.

    Returns (decision, reason) with decision "pdf", "not_pdf" or "conflict".
    The downloaded file's signature is still checked before any upload.
    """
    mime = _normalize_mime(mime_type)
    has_pdf_extension = str(name or "").strip().lower().endswith(".pdf")

    if mime == PDF_MIME_TYPE:
        return "pdf", "MIME type is application/pdf"
    if not mime or mime in _GENERIC_MIME_TYPES:
        label = f"generic MIME type {mime}" if mime else "no MIME type"
        if has_pdf_extension:
            return "pdf", f"{label}; .pdf extension fallback"
        return "not_pdf", f"{label} and no .pdf extension"
    if has_pdf_extension:
        return "conflict", f".pdf extension but MIME type is {mime}"
    return "not_pdf", f"MIME type is {mime}"


def upload_mime_type(*candidates: Any) -> str:
    """
    MIME type to upload a copied file with: the first candidate that is a
    well-formed type/subtype (lower-cased, parameters dropped) and not generic
    (application/octet-stream, binary/octet-stream); otherwise
    application/octet-stream.
    """
    for candidate in candidates:
        mime = _normalize_mime(candidate)
        if _MIME_TOKEN_PATTERN.fullmatch(mime) and mime not in _GENERIC_MIME_TYPES:
            return mime
    return FALLBACK_MIME_TYPE


def has_pdf_signature(path: str) -> bool:
    """True if a %PDF- header occurs in the first 1024 bytes (what PDF readers accept)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_PDF_SIGNATURE_WINDOW)
    except OSError:
        return False
    return _PDF_SIGNATURE in head


def has_unsupported_filename_chars(name: Any) -> bool:
    """
    True if a filename cannot round-trip through a multipart upload unchanged.

    urllib3 percent-encodes '"', CR and LF in a multipart filename, so the stored
    name would differ from the source name and every run would upload it again.
    """
    return any(ch == '"' or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in str(name or ""))


def disable_transport_retries(client: Any) -> None:
    """
    Turn off urllib3-level retries on every adapter of a Smartsheet client's session.

    SDK 4.4.0 pinned_session() mounts Retry(total=1, allowed_methods=... | {"POST"}),
    so urllib3 would silently re-send an upload POST once after a read error.

    Raises:
        RuntimeError: the session/adapters cannot be found or a retry stays enabled.
            Uploads must never run with unknown retry behaviour.
    """
    session = getattr(client, "_session", None)
    adapters = getattr(session, "adapters", None)
    if not isinstance(adapters, Mapping) or not adapters:
        raise RuntimeError(
            "Cannot disable transport retries: Smartsheet client session adapters not found"
        )
    for adapter in adapters.values():
        adapter.max_retries = Retry(total=0, read=False)
    still_retrying = [
        prefix for prefix, adapter in adapters.items()
        if getattr(getattr(adapter, "max_retries", None), "total", None) != 0
    ]
    if still_retrying:
        raise RuntimeError(
            f"Transport retries still enabled on adapters {still_retrying}; refusing to upload"
        )


def _describe_error(exc: BaseException) -> str:
    """
    Short, log-safe description of an exception.

    requests' messages embed the request URL (for downloads that is a signed
    URL), so transport errors are reduced to class name and HTTP status.
    """
    name = type(exc).__name__
    if isinstance(exc, ApiError):
        result = getattr(getattr(exc, "error", None), "result", None)
        code = getattr(result, "error_code", None)
        message = getattr(result, "message", None) or getattr(exc, "message", None)
        details = ", ".join(
            part for part in (
                f"code {code}" if code is not None else "",
                str(message) if message else "",
            ) if part
        )
        return f"{name} ({details})" if details else name
    if isinstance(exc, _TRANSPORT_ERRORS):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status is None and isinstance(exc, HttpError) and isinstance(exc.status_code, int):
            status = exc.status_code
        return f"{name} (HTTP {status})" if status is not None else name
    if isinstance(exc, (AttachmentListingError, TargetStateUnknownError, RoutingError)):
        return str(exc)
    return name


def _is_retryable_read_error(exc: BaseException) -> bool:
    if isinstance(exc, ApiError):
        return bool(getattr(exc, "should_retry", False))
    return isinstance(exc, _TRANSPORT_ERRORS)


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _upper_first(text: str) -> str:
    return text[:1].upper() + text[1:]


class _RouteLogAdapter(logging.LoggerAdapter):
    """Prefixes every log line with the route name."""

    def process(self, msg, kwargs):
        return f"[{self.extra['route']}] {msg}", kwargs


class AttachmentSyncer:
    """
    Copies FILE attachments for one pinned route from source rows to the matching
    existing target rows (matched by Work Request #). Attachment-only and additive.
    One instance per route: stats, caches and the upload ledger are per route.
    """

    def __init__(
        self,
        api_key: str,
        route: Route,
        temp_folder: str = "/tmp/smartsheet-attachments",
        max_attempts: int = 3,
        retry_delay: float = 2.0,
        upload_min_interval: float = 2.5,
        rate_limit_backoff: float = 60.0,
    ):
        """
        Initialize the AttachmentSyncer.

        Args:
            api_key: Smartsheet API key
            route: One of config.VERIFIED_ROUTES (sheet/column IDs and file rule);
                anything else raises RoutingError before any client is built
            temp_folder: Temporary folder for downloads (created only in apply mode)
            max_attempts: Maximum attempts for reads, downloads and upload POSTs
            retry_delay: Initial delay in seconds between retries (doubles each retry)
            upload_min_interval: Minimum seconds between upload POST starts
                (Smartsheet allows 30 file attachments per minute)
            rate_limit_backoff: Seconds to wait after a rate-limit rejection
        """
        # Defence in depth: refuse an unverified route before any client exists.
        # This also binds the file rule to the target sheet.
        problems = validate_route(route)
        if problems:
            raise RoutingError("; ".join(problems))
        # Fail closed on the file rule even if the route table itself had a typo:
        # an unrecognised rule must never fall through to "all files".
        if route.file_rule not in KNOWN_FILE_RULES:
            raise RoutingError(f"route {route.name!r}: unknown file rule {route.file_rule!r}")

        self.route = route
        self.source_sheet_id = route.source_sheet_id
        self.target_sheet_id = route.target_sheet_id
        self.source_match_column_id = route.source_match_column_id
        self.target_match_column_id = route.target_match_column_id
        self._pdf_only = route.file_rule == FILE_RULE_PDF_ONLY
        self._noun, self._nouns = ("PDF", "PDFs") if self._pdf_only else ("file", "files")
        self._log = _RouteLogAdapter(logger, {"route": route.name})

        # Two clients on purpose. The SDK re-sends requests silently at two layers:
        #   1. the SDK loop retries 4001-4004 responses for up to max_retry_time seconds;
        #   2. its requests session mounts a urllib3 Retry that re-sends a POST once
        #      after a read error.
        # That is fine for idempotent reads, but for the upload POST either one is a
        # blind duplicate upload. The upload client disables both layers
        # (max_retry_time=0 plus disable_transport_retries) so this class can
        # reconcile ambiguous uploads itself. Reads keep the SDK defaults.
        self.client = smartsheet.Smartsheet(api_key)
        self.client.errors_as_exceptions(True)
        self.upload_client = smartsheet.Smartsheet(api_key, max_retry_time=0)
        self.upload_client.errors_as_exceptions(True)
        disable_transport_retries(self.upload_client)

        self.temp_folder = temp_folder
        self.max_attempts = max(1, int(max_attempts))
        self.retry_delay = retry_delay
        self.upload_min_interval = upload_min_interval
        self.rate_limit_backoff = rate_limit_backoff
        self._last_upload_started: Optional[float] = None
        # (target row id, filename) pairs an upload POST was sent for in this run,
        # whatever the outcome. A same-named file is never sent to that row again.
        self._post_attempted: Set[Tuple[int, str]] = set()

        # Statistics (only "errors" drives the exit status)
        self.stats = {
            "source_rows_total": 0,
            "target_rows_total": 0,
            "source_rows_blank": 0,
            "source_wr_invalid": 0,
            "target_wr_invalid": 0,
            "wr_matched": 0,
            "wr_unmatched": 0,
            "wr_ambiguous": 0,
            "files_seen": 0,
            "unsupported_parent_excluded": 0,
            "non_pdf_excluded": 0,
            "pdf_metadata_conflicts": 0,
            "unsupported_filename_skipped": 0,
            "files_planned": 0,
            "pdf_content_verified": 0,
            "skipped_existing_filename": 0,
            "skipped_duplicate_source_filename": 0,
            "uploads_confirmed": 0,
            "uploads_unconfirmed": 0,
            "uploads_unresolved": 0,
            "uploads_name_mismatch": 0,
            "pdf_signature_rejected": 0,
            "errors": 0,
        }

    # ------------------------------------------------------------------ reads

    def _retry_read(self, operation, operation_name: str, *args, **kwargs):
        """
        Run an idempotent read with bounded retries and exponential backoff.

        Retries retryable Smartsheet API errors and transport failures; any
        other error propagates immediately.
        """
        delay = self.retry_delay
        for attempt in range(1, self.max_attempts + 1):
            try:
                return operation(*args, **kwargs)
            except Exception as exc:
                if not _is_retryable_read_error(exc) or attempt >= self.max_attempts:
                    raise
                self._log.warning(
                    f"⚠️ {operation_name} failed (attempt {attempt}/{self.max_attempts}): "
                    f"{_describe_error(exc)}"
                )
                self._log.info(f"   Retrying in {delay:.1f} seconds...")
                time.sleep(delay)
                delay *= 2

    def _list_row_file_attachments(self, sheet_id: int, row_id: int) -> List[Any]:
        """
        Complete FILE attachment listing for a row (row and row-discussion files).

        Raises:
            AttachmentListingError: the listing failed or was incomplete. The
                row's state is unknown; it must never be treated as empty.
        """
        try:
            result = self._retry_read(
                self.client.Attachments.list_row_attachments,
                f"List attachments for row {row_id} on sheet {sheet_id}",
                sheet_id,
                row_id,
                include_all=True,
            )
        except Exception as exc:
            raise AttachmentListingError(
                f"Could not list attachments for row {row_id} on sheet {sheet_id}: "
                f"{_describe_error(exc)}"
            ) from exc

        data = getattr(result, "data", None)
        # A plain list normally; the SDK leaves an empty TypedList when "data" is absent.
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            raise AttachmentListingError(
                f"Unexpected attachment listing for row {row_id} on sheet {sheet_id}"
            )
        # The API returns totalCount even for an empty row; without it the listing
        # cannot be proven complete.
        total = getattr(result, "total_count", None)
        if not isinstance(total, int) or isinstance(total, bool):
            raise AttachmentListingError(
                f"Attachment listing for row {row_id} on sheet {sheet_id} has no total count; "
                f"completeness cannot be confirmed"
            )
        if total != len(data):
            raise AttachmentListingError(
                f"Incomplete attachment listing for row {row_id} on sheet {sheet_id}: "
                f"got {len(data)} of {total}"
            )
        return [att for att in data if att.attachment_type == "FILE"]

    def _build_attachment_cache(
        self, sheet_id: int, row_ids: List[int]
    ) -> Dict[int, Optional[List[Any]]]:
        """
        Pre-fetch FILE attachment metadata (no download URLs) for many rows.

        Returns a dict row_id -> list of attachments, or None when the listing
        failed (one error is counted per failed row; never stored as []).
        """
        attachment_cache: Dict[int, Optional[List[Any]]] = {}

        self._log.info(f"📦 Pre-fetching attachment metadata for {len(row_ids)} rows...")

        for row_id in row_ids:
            try:
                attachment_cache[row_id] = self._list_row_file_attachments(sheet_id, row_id)
                self._log.debug(
                    f"Cached {len(attachment_cache[row_id])} FILE attachment metadata for row {row_id}"
                )
            except AttachmentListingError as exc:
                self._log.error(f"❌ {exc}")
                self.stats["errors"] += 1
                attachment_cache[row_id] = None

        self._log.info(f"✅ Attachment metadata cache built for {len(attachment_cache)} rows")
        return attachment_cache

    # ------------------------------------------------------------- selection

    def _select_candidates(self, source_row_id: int, file_attachments: Iterable[Any]) -> List[Any]:
        """
        Eligibility rules for cached and uncached source listings: the shared
        rules (parent type, metadata readable, filename characters) for every
        route, plus PDF metadata classification on the pdf_only route.
        """
        candidates = []
        for attachment in file_attachments:
            self.stats["files_seen"] += 1
            name = attachment.name
            parent_type = getattr(attachment, "parent_type", None)
            if not any(parent_type == allowed for allowed in _ALLOWED_PARENT_TYPES):
                self.stats["unsupported_parent_excluded"] += 1
                self._log.warning(
                    f"⚠️ Skipping '{name}' on source row {source_row_id}: not a row or "
                    f"row-discussion attachment (parent type {parent_type})"
                )
                continue
            mime_type = getattr(attachment, "mime_type", None)

            if not _normalize_mime(mime_type):
                # Resolve missing listing metadata before deciding.
                try:
                    full = self._retry_read(
                        self.client.Attachments.get_attachment,
                        f"Get attachment details for '{name}'",
                        self.source_sheet_id,
                        attachment.id,
                    )
                    mime_type = getattr(full, "mime_type", None)
                except Exception as exc:
                    self._log.error(
                        f"❌ Could not resolve metadata for '{name}' on source row "
                        f"{source_row_id}: {_describe_error(exc)}"
                    )
                    self.stats["errors"] += 1
                    continue

            if self._pdf_only:
                decision, reason = classify_pdf_metadata(name, mime_type)
                if decision == "conflict":
                    self.stats["pdf_metadata_conflicts"] += 1
                    self._log.warning(
                        f"⚠️ Skipping '{name}' on source row {source_row_id}: conflicting metadata ({reason})"
                    )
                    continue
                if decision != "pdf":
                    self.stats["non_pdf_excluded"] += 1
                    self._log.debug(f"⏭️ Not a PDF, excluded: '{name}' ({reason})")
                    continue

            if has_unsupported_filename_chars(name):
                self.stats["unsupported_filename_skipped"] += 1
                self._log.warning(
                    f"⚠️ Skipping {self._noun} {name!r} on source row {source_row_id}: the filename "
                    f"contains a quote or control character and would not be stored unchanged"
                )
                continue
            candidates.append(attachment)
        return candidates

    def _get_target_filenames(
        self,
        target_row_id: int,
        target_attachment_cache: Optional[Dict[int, Optional[Set[str]]]],
        candidate_count: int,
    ) -> Optional[Set[str]]:
        """
        Filenames of all FILE attachments on the target row, or None if unknown.
        """
        if target_attachment_cache is not None and target_row_id in target_attachment_cache:
            names = target_attachment_cache[target_row_id]
            if names is None:
                self._log.warning(
                    f"⚠️ Target row {target_row_id} attachment state is unknown; "
                    f"{candidate_count} {self._noun} candidate(s) not attempted"
                )
            return names

        try:
            attachments = self._list_row_file_attachments(self.target_sheet_id, target_row_id)
        except AttachmentListingError as exc:
            self.stats["errors"] += 1
            self._log.error(
                f"❌ Target row {target_row_id} attachment state is unknown ({exc}); "
                f"{candidate_count} {self._noun} candidate(s) not attempted"
            )
            if target_attachment_cache is not None:
                target_attachment_cache[target_row_id] = None
            return None

        names = {att.name for att in attachments}
        self._log.debug(f"Found {len(names)} existing FILE attachments on target row {target_row_id}")
        if target_attachment_cache is not None:
            target_attachment_cache[target_row_id] = names
        return names

    # ------------------------------------------------------------------ copy

    def copy_attachments_to_row(
        self,
        source_row_id: int,
        target_row_id: int,
        apply: bool = False,
        source_attachment_cache: Optional[Dict[int, Optional[List[Any]]]] = None,
        target_attachment_cache: Optional[Dict[int, Optional[Set[str]]]] = None,
    ) -> int:
        """
        Copy eligible attachments (per the route's file rule) from one source row
        to its matched target row.

        Sheet IDs are always the verified self.source_sheet_id / self.target_sheet_id.

        Args:
            source_row_id: Source row ID
            target_row_id: Matched target row ID (as returned by the API)
            apply: False = plan only (no downloads, no uploads)
            source_attachment_cache: Optional pre-fetched source listings
                (row_id -> attachments, or None for a failed listing)
            target_attachment_cache: Optional target filename sets
                (row_id -> set of names, mutated after uploads; None = unknown)

        Returns:
            Number of confirmed uploads
        """
        uploaded = 0

        try:
            if source_attachment_cache is not None and source_row_id in source_attachment_cache:
                file_attachments = source_attachment_cache[source_row_id]
                if file_attachments is None:
                    self._log.debug(f"⏭️ Source row {source_row_id} listing failed earlier; skipping")
                    return 0
            else:
                try:
                    file_attachments = self._list_row_file_attachments(
                        self.source_sheet_id, source_row_id
                    )
                except AttachmentListingError as exc:
                    self._log.error(f"❌ {exc}")
                    self.stats["errors"] += 1
                    return 0

            candidates = self._select_candidates(source_row_id, file_attachments)
            if not candidates:
                self._log.debug(f"⏭️ No {self._noun} candidates on source row {source_row_id}")
                return 0

            existing_names = self._get_target_filenames(
                target_row_id, target_attachment_cache, len(candidates)
            )
            if existing_names is None:
                return 0

            handled_names: Set[str] = set()  # planned (dry run) or uploaded in this call
            for index, attachment in enumerate(candidates):
                name = attachment.name
                if name in handled_names:
                    self.stats["skipped_duplicate_source_filename"] += 1
                    self._log.warning(
                        f"⚠️ Filename collision: '{name}' appears more than once on source row "
                        f"{source_row_id}; not adding another copy to target row {target_row_id}"
                    )
                    continue
                if name in existing_names:
                    self.stats["skipped_existing_filename"] += 1
                    self._log.info(f"⏭️ Skipping existing filename on target row {target_row_id}: {name}")
                    continue
                if (target_row_id, name) in self._post_attempted:
                    # An earlier same-named upload was unconfirmed or rejected. It may
                    # still be stored, so a second POST could create a duplicate.
                    self.stats["skipped_duplicate_source_filename"] += 1
                    self._log.warning(
                        f"⚠️ '{name}' was already sent to target row {target_row_id} in this run "
                        f"without a confirmed result; not sending another same-named file"
                    )
                    continue

                self.stats["files_planned"] += 1
                if not apply:
                    handled_names.add(name)
                    self._log.info(
                        f"📝 [DRY RUN] Would copy {self._noun} '{name}' to target row {target_row_id}"
                    )
                    continue

                try:
                    if self._copy_one_file(attachment, target_row_id, existing_names):
                        handled_names.add(name)
                        uploaded += 1
                except TargetStateUnknownError as exc:
                    remaining = len(candidates) - index - 1
                    self._log.error(
                        f"❌ {exc}; {remaining} remaining {self._noun} candidate(s) for target row "
                        f"{target_row_id} not attempted"
                    )
                    if target_attachment_cache is not None:
                        target_attachment_cache[target_row_id] = None
                    break

        except Exception as exc:
            self._log.error(
                f"❌ Error processing attachments for source row {source_row_id}: {_describe_error(exc)}"
            )
            self.stats["errors"] += 1

        return uploaded

    def _ensure_temp_folder(self) -> None:
        os.makedirs(self.temp_folder, exist_ok=True)

    def _copy_one_file(self, attachment: Any, target_row_id: int, existing_names: Set[str]) -> bool:
        """
        Download, verify (PDF signature on the pdf_only route) and upload one file.
        Returns True on a confirmed upload.
        """
        name = attachment.name
        self._ensure_temp_folder()
        # Never use the attachment name on disk (path traversal / collisions): the
        # prefix and suffix are fixed per file rule.
        prefix, suffix = ("wr-pdf-", ".pdf") if self._pdf_only else ("wr-file-", ".tmp")
        fd, file_path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=self.temp_folder)
        os.close(fd)
        try:
            mime_type = self._download_file(attachment, file_path)
            if mime_type is None:
                return False
            if self._pdf_only:
                if not has_pdf_signature(file_path):
                    self.stats["pdf_signature_rejected"] += 1
                    self.stats["errors"] += 1
                    self._log.error(
                        f"❌ '{name}' is not a PDF file (missing %PDF- signature); not uploaded"
                    )
                    return False
                self.stats["pdf_content_verified"] += 1
            return self._upload_file(name, file_path, target_row_id, existing_names, mime_type)
        finally:
            try:
                os.remove(file_path)
                self._log.debug(f"🗑️ Cleaned up temp file: {file_path}")
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._log.warning(f"Could not delete temp file {file_path}: {type(exc).__name__}")

    def _download_file(self, attachment: Any, file_path: str) -> Optional[str]:
        """
        Download with bounded attempts, each using a fresh download URL.
        Never logs the (signed) URL.

        Returns the MIME type to upload with, or None when nothing should be
        uploaded. pdf_only: always application/pdf, after the resolved metadata
        is re-checked. all_files: the resolved (get_attachment) MIME type, else
        the listing one, skipping malformed and generic values, else
        application/octet-stream.
        """
        name = attachment.name
        delay = self.retry_delay
        for attempt in range(1, self.max_attempts + 1):
            try:
                full = self._retry_read(
                    self.client.Attachments.get_attachment,
                    f"Get attachment details for '{name}'",
                    self.source_sheet_id,
                    attachment.id,
                )
            except Exception as exc:
                self._log.error(f"❌ Could not get attachment details for '{name}': {_describe_error(exc)}")
                self.stats["errors"] += 1
                return None

            if self._pdf_only:
                mime_type = getattr(full, "mime_type", None)
                if not _normalize_mime(mime_type):
                    mime_type = getattr(attachment, "mime_type", None)  # fall back to the listing
                decision, reason = classify_pdf_metadata(name, mime_type)
                if decision != "pdf":
                    self.stats["pdf_metadata_conflicts"] += 1
                    self._log.warning(f"⚠️ Skipping '{name}': resolved metadata is not PDF ({reason})")
                    return None
                upload_mime = PDF_MIME_TYPE
            else:
                # Resolved (get_attachment) type first, as production does; a generic
                # value never beats a specific one.
                upload_mime = upload_mime_type(
                    getattr(full, "mime_type", None), getattr(attachment, "mime_type", None)
                )

            url = getattr(full, "url", None)
            if url and str(url).strip():
                self._log.debug(f"📥 Downloading attachment: {name}")
                try:
                    if self._fetch_to_file(url, file_path, name, attempt):
                        return upload_mime
                except (requests.exceptions.RequestException, OSError) as exc:
                    self._log.warning(
                        f"⚠️ Download of '{name}' failed (attempt {attempt}/{self.max_attempts}): "
                        f"{_describe_error(exc)}"
                    )
            else:
                self._log.warning(
                    f"⚠️ Attachment '{name}' returned no download URL (attempt {attempt}/{self.max_attempts})"
                )

            if attempt < self.max_attempts:
                time.sleep(delay)
                delay *= 2

        self._log.error(f"❌ Download of '{name}' failed after {self.max_attempts} attempts")
        self.stats["errors"] += 1
        return None

    def _fetch_to_file(self, url: str, file_path: str, name: str, attempt: int) -> bool:
        """
        Stream one download to file_path. Returns False for a truncated body.

        When the response declares Content-Length (and no Content-Encoding, which
        would make the length refer to the encoded body) the bytes written must
        match it exactly.
        """
        written = 0
        with requests.get(url, stream=True, timeout=(10, 120)) as response:
            response.raise_for_status()
            with open(file_path, "wb") as fh:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
                        written += len(chunk)
            headers = getattr(response, "headers", None) or {}
            declared = headers.get("Content-Length")
            if declared is None or headers.get("Content-Encoding"):
                return True
            try:
                expected = int(str(declared).strip())
            except ValueError:
                expected = None
        if expected == written:
            return True
        self._log.warning(
            f"⚠️ Download of '{name}' incomplete (attempt {attempt}/{self.max_attempts}): "
            f"got {written} bytes, Content-Length {declared}"
        )
        return False

    def _throttle_upload(self) -> None:
        """Keep consecutive upload POSTs at least upload_min_interval seconds apart."""
        if self._last_upload_started is not None:
            wait = self.upload_min_interval - (time.monotonic() - self._last_upload_started)
            if wait > 0:
                time.sleep(wait)
        self._last_upload_started = time.monotonic()

    def _upload_file(
        self, name: str, file_path: str, target_row_id: int, existing_names: Set[str], mime_type: str
    ) -> bool:
        """
        Upload one downloaded (and, on the pdf_only route, verified) file to the
        verified target row with the given MIME type.

        Only a rate-limit rejection (nothing stored) is retried. Any other
        definite rejection is not retried. An ambiguous outcome is reconciled by
        re-listing the target row, and is never followed by a second POST in the
        same run: a large file may still be processing server-side, so "absent
        after one re-list" does not prove the upload failed.

        Raises:
            TargetStateUnknownError: an ambiguous upload could not be reconciled.
        """
        self._post_attempted.add((target_row_id, name))
        for attempt in range(1, self.max_attempts + 1):
            self._throttle_upload()
            self._log.info(f"📤 Uploading {name} to target row {target_row_id}")
            result = None
            with open(file_path, "rb") as fh:
                try:
                    result = self.upload_client.Attachments.attach_file_to_row(
                        self.target_sheet_id,
                        target_row_id,
                        (name, fh, mime_type),
                    )
                except Exception as exc:
                    error = exc
                else:
                    error = None

            if error is None:
                existing_names.add(name)
                self.stats["uploads_confirmed"] += 1
                self._log.info(f"✅ Successfully copied: {name}")
                self._check_stored_name(name, result, target_row_id, existing_names)
                return True

            if isinstance(error, RateLimitExceededError):
                # Definite rejection: nothing was stored.
                self._log.warning(
                    f"⚠️ Upload of '{name}' rate limited (attempt {attempt}/{self.max_attempts})"
                )
                if attempt < self.max_attempts:
                    self._log.info(f"   Waiting {self.rate_limit_backoff:.0f} seconds before retrying...")
                    time.sleep(self.rate_limit_backoff)
                continue

            if isinstance(error, ApiError) and not getattr(error, "should_retry", False):
                # Definite rejection (e.g. permissions, validation): do not retry.
                self._log.error(f"❌ Upload of '{name}' rejected: {_describe_error(error)}")
                self.stats["errors"] += 1
                return False

            return self._reconcile_ambiguous_upload(name, error, target_row_id, existing_names)

        self._log.error(f"❌ Upload of '{name}' still rate limited after {self.max_attempts} attempts")
        self.stats["errors"] += 1
        return False

    def _reconcile_ambiguous_upload(
        self, name: str, error: BaseException, target_row_id: int, existing_names: Set[str]
    ) -> bool:
        """Re-list the target row once after an ambiguous POST. Never POSTs again."""
        self._log.warning(
            f"⚠️ Upload of '{name}' to target row {target_row_id} has an ambiguous outcome: "
            f"{_describe_error(error)}; re-listing the target row (no second upload this run)"
        )
        time.sleep(self.retry_delay)
        try:
            current = self._list_row_file_attachments(self.target_sheet_id, target_row_id)
        except AttachmentListingError as exc:
            self.stats["uploads_unresolved"] += 1
            self.stats["errors"] += 1
            raise TargetStateUnknownError(
                f"UNRESOLVED upload of '{name}' to target row {target_row_id}: could not "
                f"confirm whether it was stored ({exc}); not retrying the upload"
            ) from exc

        current_names = {att.name for att in current}
        existing_names.update(current_names)
        if name in current_names:
            self.stats["uploads_confirmed"] += 1
            self._log.info(f"✅ Upload of '{name}' confirmed by reconciliation (target row re-listed)")
            return True

        self.stats["uploads_unconfirmed"] += 1
        self.stats["errors"] += 1
        self._log.error(
            f"❌ Upload of '{name}' to target row {target_row_id} is UNCONFIRMED: not listed yet "
            f"(it may still be processing). Not re-sent in this run; the next run's fresh "
            f"listing will decide"
        )
        return False

    def _check_stored_name(
        self, name: str, result: Any, target_row_id: int, existing_names: Set[str]
    ) -> None:
        """Flag an upload stored under a different name (it would be re-sent every run)."""
        stored = getattr(getattr(result, "result", None), "name", None)
        if isinstance(stored, str) and stored != name:
            existing_names.add(stored)
            self.stats["uploads_name_mismatch"] += 1
            self.stats["errors"] += 1
            self._log.warning(
                f"⚠️ '{name}' was stored on target row {target_row_id} as {stored!r}; "
                f"the filename duplicate check will not recognise it on the next run"
            )

    # ------------------------------------------------------------------ sync

    def _preflight_sheet(
        self, sheet: Any, expected_sheet_id: int, match_column_id: int, expected_title: str, label: str
    ) -> None:
        """
        Fail before any write if the loaded sheet is not the verified one, or its
        match column is missing or not titled exactly as pinned for this route.
        """
        route = f"route {self.route.name!r}"
        actual_id = _as_int(getattr(sheet, "id", None))
        if actual_id != expected_sheet_id:
            raise RoutingError(
                f"{route}: {label} sheet mismatch: expected {expected_sheet_id}, API returned {actual_id}"
            )
        columns = getattr(sheet, "columns", None) or []
        column = next((col for col in columns if _as_int(col.id) == match_column_id), None)
        if column is None:
            raise RoutingError(
                f"{route}: {label} match column {match_column_id} not found in sheet {expected_sheet_id}"
            )
        title = str(column.title or "").strip()
        if title != expected_title:
            raise RoutingError(
                f"{route}: {label} match column {match_column_id} is titled {title!r}, "
                f"expected {expected_title!r}"
            )
        rows = getattr(sheet, "rows", None) or []
        total = getattr(sheet, "total_row_count", None)
        if isinstance(total, int) and not isinstance(total, bool) and total != len(rows):
            raise RoutingError(
                f"{route}: {label} sheet {expected_sheet_id} loaded incompletely: "
                f"{len(rows)} of {total} rows"
            )
        self._log.info(
            f"✅ {label.capitalize()} preflight OK: sheet {actual_id}, column {match_column_id} "
            f"'{expected_title}', {len(rows)} rows"
        )

    @staticmethod
    def _index_rows(rows: Iterable[Any], match_column_id: int):
        """
        Map valid Work Request # keys to row IDs (sheet order).

        Returns (index, blank_count, invalid_count, invalid_samples).
        """
        index: Dict[int, List[int]] = {}
        blank = 0
        invalid = 0
        samples: List[Any] = []
        for row in rows:
            value = None
            for cell in row.cells:
                if _as_int(cell.column_id) == match_column_id:
                    value = cell.value
                    break
            key, status = normalize_work_request(value)
            if status == "valid":
                index.setdefault(key, []).append(row.id)
            elif status == "blank":
                blank += 1
            else:
                invalid += 1
                if len(samples) < 5:
                    samples.append(value)
        return index, blank, invalid, samples

    def sync_attachments(self, apply: bool = False) -> Dict[str, int]:
        """
        Main sync method for this route: source -> target, per the route's file rule.

        Args:
            apply: False (default) = dry run: matching and metadata planning only,
                no downloads, no uploads. True = copy eligible files.

        Returns:
            Dictionary with sync statistics
        """
        mode = "APPLY" if apply else "DRY RUN"
        self._log.info("=" * 60)
        rule = "PDF files only" if self._pdf_only else "all file attachments"
        self._log.info(
            f"🚀 Starting Smartsheet Attachment Sync, route '{self.route.name}' "
            f"({rule}): sheet {self.source_sheet_id} → sheet {self.target_sheet_id} ({mode})"
        )
        self._log.info("=" * 60)

        try:
            # Load sheets
            self._log.info(f"📋 Loading source sheet: {self.source_sheet_id}")
            source_sheet = self._retry_read(
                self.client.Sheets.get_sheet, "Load source sheet", self.source_sheet_id
            )
            self._log.info(f"📋 Loading target sheet: {self.target_sheet_id}")
            target_sheet = self._retry_read(
                self.client.Sheets.get_sheet, "Load target sheet", self.target_sheet_id
            )

            # Destination checks before any write
            self._preflight_sheet(
                source_sheet, self.source_sheet_id, self.source_match_column_id,
                self.route.source_match_column_title, "source",
            )
            self._preflight_sheet(
                target_sheet, self.target_sheet_id, self.target_match_column_id,
                self.route.target_match_column_title, "target",
            )

            source_rows = list(source_sheet.rows or [])
            target_rows = list(target_sheet.rows or [])
            self.stats["source_rows_total"] = len(source_rows)
            self.stats["target_rows_total"] = len(target_rows)

            # Build Work Request # indexes
            self._log.info(
                f"🗺️ Indexing rows by '{self.route.source_match_column_title}' (source) and "
                f"'{self.route.target_match_column_title}' (target)..."
            )
            source_index, source_blank, source_invalid, source_samples = self._index_rows(
                source_rows, self.source_match_column_id
            )
            target_index, target_blank, target_invalid, target_samples = self._index_rows(
                target_rows, self.target_match_column_id
            )
            self.stats["source_rows_blank"] = source_blank
            self.stats["source_wr_invalid"] = source_invalid
            self.stats["target_wr_invalid"] = target_invalid

            self._log.info(
                f"📊 Source: {len(source_rows)} rows, {len(source_index)} distinct valid WRs, "
                f"{source_blank} blank, {source_invalid} invalid"
                + (f" (e.g. {source_samples})" if source_samples else "")
            )
            self._log.info(
                f"📊 Target: {len(target_rows)} rows, {len(target_index)} distinct valid WRs, "
                f"{target_blank} blank, {target_invalid} invalid"
                + (f" (e.g. {target_samples})" if target_samples else "")
            )
            if not target_index:
                self._log.info(
                    "ℹ️ Target has no rows with a valid Work Request # - nothing to do"
                )

            # Match source WRs to target rows
            pairs: List[Tuple[int, int, int]] = []
            for key, source_row_ids in source_index.items():
                target_row_ids = target_index.get(key, [])
                if len(source_row_ids) > 1 or len(target_row_ids) > 1:
                    self.stats["wr_ambiguous"] += 1
                    self._log.warning(
                        f"⚠️ Ambiguous WR {key}: source rows {source_row_ids}, "
                        f"target rows {target_row_ids}; skipped"
                    )
                    continue
                if not target_row_ids:
                    self.stats["wr_unmatched"] += 1
                    self._log.debug(f"⏭️ No matching target row for WR {key}, skipping")
                    continue
                self.stats["wr_matched"] += 1
                pairs.append((key, source_row_ids[0], target_row_ids[0]))

            self._log.info(
                f"🔗 WRs matched: {self.stats['wr_matched']}, unmatched: {self.stats['wr_unmatched']}, "
                f"ambiguous: {self.stats['wr_ambiguous']}"
            )

            # Read-only planning: source attachment metadata for matched rows
            source_attachment_cache = self._build_attachment_cache(
                self.source_sheet_id, [source_row_id for _, source_row_id, _ in pairs]
            )

            self._log.info("🔄 Starting attachment sync...")
            for key, source_row_id, target_row_id in pairs:
                self._log.info(
                    f"🔗 Processing WR {key}: source row {source_row_id} → target row {target_row_id}"
                )
                copied = self.copy_attachments_to_row(
                    source_row_id,
                    target_row_id,
                    apply=apply,
                    source_attachment_cache=source_attachment_cache,
                )
                if copied > 0:
                    self._log.info(f"✅ Copied {copied} {self._noun}(s) for WR {key}")

            self._log_summary(mode)

        except Exception as e:
            self._log.error(f"❌ Fatal error during sync: {_describe_error(e)}")
            self.stats["errors"] += 1
            raise

        return self.stats

    def _log_summary(self, mode: str) -> None:
        s = self.stats
        self._log.info("=" * 60)
        self._log.info(f"📊 SYNC SUMMARY, route '{self.route.name}' ({mode})")
        self._log.info("=" * 60)
        self._log.info(f"Source rows: {s['source_rows_total']} (blank WR: {s['source_rows_blank']}, invalid WR: {s['source_wr_invalid']})")
        self._log.info(f"Target rows: {s['target_rows_total']} (invalid WR: {s['target_wr_invalid']})")
        self._log.info(f"WRs matched: {s['wr_matched']}")
        self._log.info(f"WRs unmatched (no target row): {s['wr_unmatched']}")
        self._log.info(f"WRs ambiguous (skipped): {s['wr_ambiguous']}")
        self._log.info(f"FILE attachments seen: {s['files_seen']}")
        self._log.info(f"Excluded (not a row/discussion attachment): {s['unsupported_parent_excluded']}")
        nouns = _upper_first(self._nouns)
        if self._pdf_only:
            self._log.info(f"Non-PDF files excluded: {s['non_pdf_excluded']}")
            self._log.info(f"PDF metadata conflicts (skipped): {s['pdf_metadata_conflicts']}")
        self._log.info(f"{nouns} skipped (unsupported filename characters): {s['unsupported_filename_skipped']}")
        self._log.info(f"{nouns} planned (new filenames): {s['files_planned']}")
        if self._pdf_only:
            self._log.info(f"PDFs content-verified (%PDF- signature): {s['pdf_content_verified']}")
            self._log.info(f"PDFs rejected by signature check: {s['pdf_signature_rejected']}")
        self._log.info(f"Skipped (filename already on target row): {s['skipped_existing_filename']}")
        self._log.info(f"Skipped (duplicate filename on source row): {s['skipped_duplicate_source_filename']}")
        self._log.info(f"Uploads confirmed: {s['uploads_confirmed']}")
        self._log.info(f"Uploads unconfirmed (not re-sent; next run decides): {s['uploads_unconfirmed']}")
        self._log.info(f"Uploads unresolved (target state unknown): {s['uploads_unresolved']}")
        self._log.info(f"Uploads stored under a different name: {s['uploads_name_mismatch']}")
        self._log.info(f"Errors: {s['errors']}")
        self._log.info("=" * 60)

        if s["errors"] > 0:
            self._log.warning(f"⚠️ Sync completed with {s['errors']} error(s)")
        else:
            self._log.info("✅ Sync completed successfully!")
