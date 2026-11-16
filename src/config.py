"""
Configuration for Smartsheet Attachment Sync.
Uses environment variables for sensitive data.

Routing (source/target sheet and Work Request # column IDs, plus the file rule
of each target) is pinned in code as VERIFIED_ROUTES. It is not read from the
environment: retargeting is a reviewed code change, not a config override.
"""
import os
from dataclasses import dataclass, fields
from typing import Any, List, Mapping, Optional


# File rules: which FILE attachments a route copies
FILE_RULE_ALL = "all_files"      # every FILE attachment that passes the shared rules
FILE_RULE_PDF_ONLY = "pdf_only"  # PDF files only (metadata + %PDF- signature)
# Every recognised rule. Anything else is rejected (fail closed), never treated as "all files".
KNOWN_FILE_RULES = (FILE_RULE_ALL, FILE_RULE_PDF_ONLY)


@dataclass(frozen=True)
class Route:
    """
    One pinned source -> target attachment route, matched by Work Request #.

    The match column titles are the exact titles each sheet's match column must
    carry; preflight checks them against the live sheets before any write.
    """

    name: str
    source_sheet_id: int
    target_sheet_id: int
    source_match_column_id: int
    target_match_column_id: int
    file_rule: str
    source_match_column_title: str
    target_match_column_title: str


# Verified routes, run in this order. The Master Schedule runs first so the large
# first Blank Copy backfill cannot delay it.
VERIFIED_ROUTES = (
    Route(
        name="master-schedule",
        source_sheet_id=1553121697288068,         # Project List
        target_sheet_id=4850043816202116,         # Master Schedule
        source_match_column_id=8022925527175044,  # Project List: Work Request #
        target_match_column_id=1437881091182468,  # Master Schedule: WR (primary column)
        file_rule=FILE_RULE_ALL,
        source_match_column_title="Work Request #",
        target_match_column_title="WR",
    ),
    Route(
        name="blank-copy",
        source_sheet_id=1553121697288068,         # Project List
        target_sheet_id=573385022328708,          # Project List - Blank Copy
        source_match_column_id=8022925527175044,  # Project List: Work Request #
        target_match_column_id=7068369005875076,  # Blank Copy: Work Request #
        file_rule=FILE_RULE_PDF_ONLY,
        source_match_column_title="Work Request #",
        target_match_column_title="Work Request #",
    ),
)

# Legacy routing variables. They are ignored; main.py warns when any is set.
ROUTING_ENV_VARS = (
    "SOURCE_SHEET_ID",
    "TARGET_SHEET_ID",
    "SOURCE_MATCH_COLUMN_ID",
    "TARGET_MATCH_COLUMN_ID",
)

_ID_FIELDS = (
    "source_sheet_id",
    "target_sheet_id",
    "source_match_column_id",
    "target_match_column_id",
)

# API Key (must be set via environment variable)
SMARTSHEET_API_KEY = os.getenv("SMARTSHEET_API_KEY")

# Temp folder for attachment downloads
TEMP_DOWNLOAD_FOLDER = os.getenv("TEMP_DOWNLOAD_FOLDER", "/tmp/smartsheet-attachments")


def routing_env_vars_set(environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """Names of legacy routing variables that hold a non-blank value (they are ignored)."""
    env = os.environ if environ is None else environ
    return [name for name in ROUTING_ENV_VARS if (env.get(name) or "").strip()]


def validate_route(route: Any) -> List[str]:
    """
    Check a route against VERIFIED_ROUTES.

    Pure function (no API calls). Returns a list of human-readable problems; an
    empty list means the route equals one of VERIFIED_ROUTES in every field
    (IDs, file rule and both match column titles).
    """
    if not isinstance(route, Route):
        return [f"route must be a Route, got {type(route).__name__}"]

    label = f"route {route.name!r}"
    problems = []
    # Checked on its own, not only via the table comparison below, so a typo in a
    # pinned route can never turn into a rule the syncer does not know.
    if route.file_rule not in KNOWN_FILE_RULES:
        problems.append(
            f"{label}: unknown file rule {route.file_rule!r}; expected one of {list(KNOWN_FILE_RULES)}"
        )
    problems.extend(
        f"{label}: {field} must be an integer, got {getattr(route, field)!r}"
        for field in _ID_FIELDS
        if type(getattr(route, field)) is not int  # bool and float are rejected too
    )
    if problems:
        return problems

    if route.source_sheet_id == route.target_sheet_id:
        problems.append(
            f"{label}: source and target are the same sheet ({route.source_sheet_id}); "
            f"same-sheet routing is not allowed"
        )

    verified = next(
        (r for r in VERIFIED_ROUTES if r.target_sheet_id == route.target_sheet_id), None
    )
    if verified is None:
        problems.append(
            f"{label}: target sheet {route.target_sheet_id} is not a verified target"
        )
        return problems

    if route.file_rule != verified.file_rule:
        problems.append(
            f"{label}: file rule {route.file_rule!r} is wrong for target sheet "
            f"{route.target_sheet_id}; expected {verified.file_rule!r}"
        )
    for field in fields(Route):
        if field.name in ("target_sheet_id", "file_rule"):
            continue
        expected, actual = getattr(verified, field.name), getattr(route, field.name)
        if actual != expected:
            problems.append(f"{label}: {field.name}: expected {expected!r}, got {actual!r}")

    if not problems and route not in VERIFIED_ROUTES:  # defensive; the loop covers every field
        problems.append(f"{label} is not a verified route")
    return problems
