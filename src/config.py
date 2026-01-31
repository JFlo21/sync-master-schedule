"""
Configuration for Smartsheet Attachment Sync.
Uses environment variables for sensitive data.
"""
import os


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


# Sheet IDs
SOURCE_SHEET_ID = os.getenv("SOURCE_SHEET_ID") or "1553121697288068"
TARGET_SHEET_ID = os.getenv("TARGET_SHEET_ID") or "4850043816202116"

# Column IDs for row matching criteria
SOURCE_MATCH_COLUMN_ID = _get_int_env_var("SOURCE_MATCH_COLUMN_ID", "8022925527175044")
TARGET_MATCH_COLUMN_ID = _get_int_env_var("TARGET_MATCH_COLUMN_ID", "1437881091182468")

# API Key (must be set via environment variable)
SMARTSHEET_API_KEY = os.getenv("SMARTSHEET_API_KEY")

# Temp folder for attachment downloads
TEMP_DOWNLOAD_FOLDER = os.getenv("TEMP_DOWNLOAD_FOLDER", "/tmp/smartsheet-attachments")
