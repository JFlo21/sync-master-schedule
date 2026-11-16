"""
Entry point for Smartsheet Attachment Sync.

Copies PDF file attachments from Project List rows to the matching existing rows
of Project List - Blank Copy (matched by Work Request #).

    python main.py --dry-run   # plan only (default): no downloads, no uploads
    python main.py --apply     # download, verify and upload PDFs
"""
import argparse
import logging
import os
import sys
from dotenv import load_dotenv

# Load environment variables from .env file (for local development)
load_dotenv()

from src.config import (
    SMARTSHEET_API_KEY,
    SOURCE_SHEET_ID,
    TARGET_SHEET_ID,
    SOURCE_MATCH_COLUMN_ID,
    TARGET_MATCH_COLUMN_ID,
    TEMP_DOWNLOAD_FOLDER,
    validate_routing,
)
from src.attachment_sync import AttachmentSyncer, RoutingError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
# The SDK logs request URLs at INFO and full response bodies at DEBUG (a
# get_attachment response contains a signed download URL). Keep both quiet even
# if the root level is raised to DEBUG.
logging.getLogger("smartsheet").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


def parse_args(argv=None):
    """Parse the explicit dry-run / apply mode."""
    parser = argparse.ArgumentParser(
        description=(
            "Copy PDF file attachments from Project List rows to the matching existing rows "
            "of Project List - Blank Copy, matched by Work Request #. Attachment-only: "
            "rows and cells are never written. Defaults to a dry run."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only (default): match rows and list PDF candidates; no downloads, no uploads.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Download, verify and upload eligible PDFs to the matching target rows.",
    )
    return parser.parse_args(argv)


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

    for problem in validate_routing(
        SOURCE_SHEET_ID, TARGET_SHEET_ID, SOURCE_MATCH_COLUMN_ID, TARGET_MATCH_COLUMN_ID
    ):
        errors.append(f"❌ Routing: {problem}")

    if errors:
        logger.error("Configuration validation failed:")
        for error in errors:
            logger.error(error)
        logger.error("\nPlease set the required environment variables.")
        logger.error("For local development: see .env.example")
        logger.error("For GitHub Actions: only SMARTSHEET_API_KEY is a secret; routing IDs are set in the workflow")
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


def main(argv=None) -> int:
    """
    Main entry point for the attachment sync application.

    Returns:
        Process exit status: 0 when no unresolved errors, 1 otherwise,
        130 when interrupted.
    """
    args = parse_args(argv)
    apply = bool(args.apply)

    # A workflow that passes no flag would otherwise become a silent daily dry run.
    if not args.apply and not args.dry_run and os.environ.get("GITHUB_ACTIONS") == "true":
        logger.error(
            "❌ No mode flag in GitHub Actions; the workflow must pass --dry-run or --apply"
        )
        return 1

    logger.info("🚀 Smartsheet Attachment Sync - Starting")
    if apply:
        logger.info("Mode: APPLY (eligible PDFs will be uploaded to the target sheet)")
    elif args.dry_run:
        logger.info("Mode: DRY RUN (no downloads, no uploads)")
    else:
        logger.info("Mode: DRY RUN (default - no mode flag given; use --apply to upload)")

    # Validate configuration
    if not validate_config():
        logger.error("❌ Exiting due to configuration errors")
        return 1

    try:
        syncer = AttachmentSyncer(
            api_key=SMARTSHEET_API_KEY,
            source_sheet_id=SOURCE_SHEET_ID,
            target_sheet_id=TARGET_SHEET_ID,
            source_match_column_id=SOURCE_MATCH_COLUMN_ID,
            target_match_column_id=TARGET_MATCH_COLUMN_ID,
            temp_folder=TEMP_DOWNLOAD_FOLDER
        )

        # Run sync
        stats = syncer.sync_attachments(apply=apply)

    except KeyboardInterrupt:
        logger.info("\n⚠️ Sync interrupted by user")
        return 130
    except RoutingError as e:
        logger.error(f"❌ Routing check failed, nothing was written: {e}")
        return 1
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        return 1

    errors = stats.get("errors", 0)
    if errors == 0:
        logger.info("✅ Sync completed with no errors")
        return 0

    # Expected skips (invalid placeholders, unmatched WRs, existing filenames)
    # are not errors; anything counted here is an unresolved operational error.
    logger.error(f"❌ Sync finished with {errors} unresolved error(s) - exiting with status 1")
    return 1


if __name__ == "__main__":
    sys.exit(main())
