"""
Entry point for Smartsheet Attachment Sync.

Runs the two pinned routes (src/config.py VERIFIED_ROUTES) in order, both reading
Project List and matching existing target rows by Work Request # (the match
column is titled "WR" on the Master Schedule):

    1. master-schedule: all file attachments -> Master Schedule
    2. blank-copy:      PDF files only       -> Project List - Blank Copy

    python main.py --dry-run   # plan only (default): no downloads, no uploads
    python main.py --apply     # download, verify and upload eligible files
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
    TEMP_DOWNLOAD_FOLDER,
    VERIFIED_ROUTES,
    routing_env_vars_set,
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
            "Copy file attachments from Project List rows to the matching existing rows "
            "of two target sheets, matched by Work Request #. Routes run in order: "
            "master-schedule (all file attachments, Master Schedule), then "
            "blank-copy (PDF files only, Project List - Blank Copy). Routing is pinned "
            "in code. Attachment-only: rows and cells are never written. Defaults to a "
            "dry run."
        )
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only (default): match rows and list candidates on both routes; "
             "no downloads, no uploads.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Download, verify and upload eligible files to the matching target rows "
             "on both routes.",
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

    if errors:
        logger.error("Configuration validation failed:")
        for error in errors:
            logger.error(error)
        logger.error("\nPlease set the required environment variables.")
        logger.error("For local development: see .env.example")
        logger.error("For GitHub Actions: SMARTSHEET_API_KEY is the only secret; routing is pinned in code")
        return False

    # Log configuration (without sensitive data)
    logger.info("Configuration loaded:")
    for route in VERIFIED_ROUTES:
        logger.info(
            f"  Route {route.name}: sheet {route.source_sheet_id} (column "
            f"{route.source_match_column_id}) → sheet {route.target_sheet_id} (column "
            f"{route.target_match_column_id}), file rule {route.file_rule}"
        )
    logger.info(f"  Temp Download Folder: {TEMP_DOWNLOAD_FOLDER}")
    logger.info(f"  API Key: {'*' * 20} (hidden)")

    return True


def main(argv=None) -> int:
    """
    Main entry point for the attachment sync application.

    Runs every route in VERIFIED_ROUTES in order. A failure in one route is
    logged and counted, and the next route still runs.

    Returns:
        Process exit status: 0 when every route finished with no unresolved
        errors, 1 otherwise, 130 when interrupted.
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
        logger.info("Mode: APPLY (eligible files will be uploaded to the target sheets)")
    elif args.dry_run:
        logger.info("Mode: DRY RUN (no downloads, no uploads)")
    else:
        logger.info("Mode: DRY RUN (default - no mode flag given; use --apply to upload)")

    ignored = routing_env_vars_set()
    if ignored:
        logger.warning(
            f"⚠️ Ignoring routing environment variables {', '.join(ignored)}: "
            f"routing is pinned in code (src/config.py VERIFIED_ROUTES)"
        )

    # Validate configuration
    if not validate_config():
        logger.error("❌ Exiting due to configuration errors")
        return 1

    results = []  # (route name, ok, outcome)
    for route in VERIFIED_ROUTES:
        try:
            syncer = AttachmentSyncer(
                api_key=SMARTSHEET_API_KEY,
                route=route,
                temp_folder=TEMP_DOWNLOAD_FOLDER,
            )
            stats = syncer.sync_attachments(apply=apply)
        except KeyboardInterrupt:
            logger.info("\n⚠️ Sync interrupted by user")
            return 130
        except RoutingError as e:
            logger.error(f"❌ [{route.name}] Routing check failed, nothing was written for this route: {e}")
            results.append((route.name, False, "routing check failed"))
            continue
        except Exception as e:
            logger.error(f"❌ [{route.name}] Fatal error: {e}", exc_info=True)
            results.append((route.name, False, f"fatal error ({type(e).__name__})"))
            continue

        # Expected skips (invalid placeholders, unmatched WRs, existing filenames)
        # are not errors; anything counted here is an unresolved operational error.
        errors = stats.get("errors", 0)
        if errors == 0:
            results.append((route.name, True, "completed with no errors"))
        else:
            results.append((route.name, False, f"{errors} unresolved error(s)"))

    logger.info("=" * 60)
    for name, ok, outcome in results:
        if ok:
            logger.info(f"✅ Route {name}: {outcome}")
        else:
            logger.error(f"❌ Route {name}: {outcome}")

    failed = sum(1 for _, ok, _ in results if not ok)
    if failed == 0:
        logger.info(f"✅ All {len(results)} routes completed with no errors")
        return 0

    logger.error(
        f"❌ {failed} of {len(results)} route(s) failed or had errors - exiting with status 1"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
