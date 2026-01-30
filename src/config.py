"""
Configuration for Smartsheet Attachment Sync.
Uses environment variables for sensitive data.
"""
import os

# Sheet IDs
SOURCE_SHEET_ID = os.getenv("SOURCE_SHEET_ID") or "1553121697288068"
TARGET_SHEET_ID = os.getenv("TARGET_SHEET_ID") or "4850043816202116"

# Column IDs for row matching criteria
SOURCE_MATCH_COLUMN_ID = int(os.getenv("SOURCE_MATCH_COLUMN_ID") or "8022925527175044")
TARGET_MATCH_COLUMN_ID = int(os.getenv("TARGET_MATCH_COLUMN_ID") or "1437881091182468")

# API Key (must be set via environment variable)
SMARTSHEET_API_KEY = os.getenv("SMARTSHEET_API_KEY")

# Temp folder for attachment downloads
TEMP_DOWNLOAD_FOLDER = os.getenv("TEMP_DOWNLOAD_FOLDER", "/tmp/smartsheet-attachments")
