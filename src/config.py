"""
Configuration for Smartsheet Attachment Sync.
Uses environment variables for sensitive data.

Routing (source/target sheet and Work Request # column IDs) is pinned to the
verified values below. Environment variables may still be set, but they must
equal the verified values: retargeting is a reviewed code change, not a config
override.
"""
import os
from types import MappingProxyType
from typing import List


def _get_int_env_var(name: str, default: str) -> int:
    """
    Retrieve an environment variable expected to be an integer.

    If the variable is unset or only contains whitespace, the provided
    default string is used instead. If the resulting value cannot be
    converted to an integer, a clear ValueError is raised.
    """
    raw_value = os.getenv(name)

    if raw_value is None or raw_value.strip() == "":
        return int(default)

    try:
        return int(raw_value)
    except ValueError as exc:
        raise ValueError(
            f"Environment variable {name} must be an integer, got {raw_value!r}."
        ) from exc


# Title both match columns must carry (checked against the live sheets before any write)
MATCH_COLUMN_TITLE = "Work Request #"

# Verified routing: Project List -> Project List - Blank Copy, matched by Work Request #
VERIFIED_ROUTING = MappingProxyType({
    "source_sheet_id": 1553121697288068,        # Project List
    "target_sheet_id": 573385022328708,         # Project List - Blank Copy
    "source_match_column_id": 8022925527175044,  # Project List: Work Request #
    "target_match_column_id": 7068369005875076,  # Blank Copy: Work Request #
})

# Old target (match column 1437881091182468). The job must never upload there.
RETIRED_TARGET_SHEET_IDS = frozenset({4850043816202116})

# Sheet IDs
SOURCE_SHEET_ID = _get_int_env_var("SOURCE_SHEET_ID", str(VERIFIED_ROUTING["source_sheet_id"]))
TARGET_SHEET_ID = _get_int_env_var("TARGET_SHEET_ID", str(VERIFIED_ROUTING["target_sheet_id"]))

# Column IDs for row matching criteria (Work Request # on both sheets)
SOURCE_MATCH_COLUMN_ID = _get_int_env_var(
    "SOURCE_MATCH_COLUMN_ID", str(VERIFIED_ROUTING["source_match_column_id"])
)
TARGET_MATCH_COLUMN_ID = _get_int_env_var(
    "TARGET_MATCH_COLUMN_ID", str(VERIFIED_ROUTING["target_match_column_id"])
)

# API Key (must be set via environment variable)
SMARTSHEET_API_KEY = os.getenv("SMARTSHEET_API_KEY")

# Temp folder for attachment downloads
TEMP_DOWNLOAD_FOLDER = os.getenv("TEMP_DOWNLOAD_FOLDER", "/tmp/smartsheet-attachments")


def validate_routing(
    source_sheet_id,
    target_sheet_id,
    source_match_column_id,
    target_match_column_id,
) -> List[str]:
    """
    Check the effective routing against the verified routing.

    Pure function (no API calls). Returns a list of human-readable problems;
    an empty list means the routing is the verified one.
    """
    effective = {
        "source_sheet_id": source_sheet_id,
        "target_sheet_id": target_sheet_id,
        "source_match_column_id": source_match_column_id,
        "target_match_column_id": target_match_column_id,
    }
    problems = []
    parsed = {}
    for key, value in effective.items():
        if isinstance(value, bool):
            problems.append(f"{key} must be an integer, got {value!r}")
            continue
        try:
            parsed[key] = int(str(value).strip())
        except (TypeError, ValueError):
            problems.append(f"{key} must be an integer, got {value!r}")

    source = parsed.get("source_sheet_id")
    target = parsed.get("target_sheet_id")
    if source is not None and source == target:
        problems.append(
            f"source and target are the same sheet ({source}); same-sheet routing is not allowed"
        )
    if target in RETIRED_TARGET_SHEET_IDS:
        problems.append(
            f"target sheet {target} is the old (retired) target; the job must not upload there"
        )
    for key, expected in VERIFIED_ROUTING.items():
        if key in parsed and parsed[key] != expected:
            problems.append(f"{key}: expected {expected}, effective {parsed[key]}")
    return problems
