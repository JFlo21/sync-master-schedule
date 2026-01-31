"""
Entry point for Smartsheet Attachment Sync.
"""
import logging
import sys
import os
from dotenv import load_dotenv

# Load environment variables from .env file (for local development)
load_dotenv()

from src.config import (
    SMARTSHEET_API_KEY,
    SOURCE_SHEET_ID,
    TARGET_SHEET_ID,
    SOURCE_MATCH_COLUMN_ID,
    TARGET_MATCH_COLUMN_ID,
    TEMP_DOWNLOAD_FOLDER
)
from src.attachment_sync import AttachmentSyncer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def validate_config():
    """
    Validate required configuration values.
    
    Returns:
        bool: True if configuration is valid, False otherwise
    """
    errors = []

    if not SMARTSHEET_API_KEY or not SMARTSHEET_API_KEY.strip():
        errors.append("❌ SMARTSHEET_API_KEY is required but not set")
    elif len(SMARTSHEET_API_KEY.strip()) < 20:
        errors.append("❌ SMARTSHEET_API_KEY appears to be invalid (too short)")

    if not SOURCE_SHEET_ID or not SOURCE_SHEET_ID.strip():
        errors.append("❌ SOURCE_SHEET_ID is required but not set")

    if not TARGET_SHEET_ID or not TARGET_SHEET_ID.strip():
        errors.append("❌ TARGET_SHEET_ID is required but not set")

    if errors:
        logger.error("Configuration validation failed:")
        for error in errors:
            logger.error(error)
        logger.error("\nPlease set the required environment variables.")
        logger.error("For local development: see .env.example")
        logger.error("For GitHub Actions: set repository secrets in Settings → Secrets and variables → Actions")
        return False

    # Log configuration (without sensitive data)
    logger.info("Configuration loaded:")
    logger.info(f"  Source Sheet ID: {SOURCE_SHEET_ID}")
    logger.info(f"  Target Sheet ID: {TARGET_SHEET_ID}")
    logger.info(f"  Source Match Column ID: {SOURCE_MATCH_COLUMN_ID}")
    logger.info(f"  Target Match Column ID: {TARGET_MATCH_COLUMN_ID}")
    logger.info(f"  Temp Download Folder: {TEMP_DOWNLOAD_FOLDER}")
    logger.info(f"  API Key: {'*' * 20} (hidden)")

    return True


def main():
    """
    Main entry point for the attachment sync application.
    """
    logger.info("🚀 Smartsheet Attachment Sync - Starting")
    
    # Validate configuration
    if not validate_config():
        logger.error("❌ Exiting due to configuration errors")
        sys.exit(1)

    try:
        # Initialize syncer (it will create temp folder in __init__)
        syncer = AttachmentSyncer(
            api_key=SMARTSHEET_API_KEY,
            source_sheet_id=SOURCE_SHEET_ID,
            target_sheet_id=TARGET_SHEET_ID,
            source_match_column_id=SOURCE_MATCH_COLUMN_ID,
            target_match_column_id=TARGET_MATCH_COLUMN_ID,
            temp_folder=TEMP_DOWNLOAD_FOLDER
        )

        # Run sync
        stats = syncer.sync_attachments()

        # Exit with error code if there were errors
        if stats.get("errors", 0) > 0:
            logger.warning(f"⚠️ Sync completed with {stats['errors']} error(s)")
            sys.exit(1)
        else:
            logger.info("✅ Sync completed successfully!")
            sys.exit(0)

    except KeyboardInterrupt:
        logger.info("\n⚠️ Sync interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
