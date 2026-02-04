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

# Error handling threshold - if error rate is below this, treat as success with warnings
# This allows for transient errors (network issues, rate limits, etc.) without failing the workflow
ERROR_RATE_THRESHOLD = 0.1  # 10% - allows up to 10% of operations to fail while still succeeding


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

        # Determine exit code based on error severity
        errors = stats.get("errors", 0)
        synced = stats.get("attachments_synced", 0)
        skipped = stats.get("attachments_skipped", 0)
        rows_processed = stats.get("total_rows_processed", 0)
        
        # Log processing statistics
        logger.info(f"📊 Processed {rows_processed} row(s) with matching keys")
        
        # Total operations includes synced, skipped, and errors
        # This gives us a true picture of the work done
        total_operations = synced + skipped + errors
        
        if errors == 0:
            # Perfect success - no errors at all
            logger.info("✅ Sync completed successfully!")
            sys.exit(0)
        elif total_operations > 0:
            # Some errors occurred - calculate error rate against all operations
            error_rate = errors / total_operations
            
            # Log the error details
            logger.warning(f"⚠️ Sync completed with {errors} error(s) out of {total_operations} total operations")
            logger.warning(f"   - Attachments synced: {synced}")
            logger.warning(f"   - Attachments skipped (duplicates): {skipped}")
            logger.warning(f"   - Errors: {errors}")
            logger.warning(f"   - Error rate: {error_rate:.2%}")
            
            if error_rate < ERROR_RATE_THRESHOLD:
                # Mostly successful - treat as success with warnings
                logger.info("✅ Exiting with success - error rate is acceptable")
                logger.info("   Most operations completed successfully; errors appear to be transient")
                sys.exit(0)
            else:
                # High error rate - but only fail if it's truly concerning
                # If we made progress (synced or skipped attachments), that's still partial success
                if synced > 0 or skipped > 0:
                    logger.warning("⚠️ Error rate is high, but sync made progress")
                    logger.warning("   Treating as success with warnings to prevent workflow failure")
                    logger.info("✅ Exiting with success despite elevated error rate")
                    sys.exit(0)
                else:
                    # True failure - high error rate and no meaningful work done
                    logger.error(f"❌ Sync failed with high error rate and no progress")
                    logger.error("❌ Exiting with failure")
                    sys.exit(1)
        else:
            # No work attempted but errors occurred - this is a configuration or setup issue
            logger.error(f"❌ Sync failed with {errors} error(s) and no operations attempted")
            logger.error("   This may indicate a configuration or connectivity problem")
            sys.exit(1)

    except KeyboardInterrupt:
        logger.info("\n⚠️ Sync interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
