"""
Core attachment synchronization logic for Smartsheet.
"""
import logging
import os
import time
import smartsheet
import requests
from typing import Dict, Set, Optional, List, Any

logger = logging.getLogger(__name__)


class AttachmentSyncer:
    """
    Synchronizes attachments between two Smartsheet sheets based on matching column criteria.
    """

    def __init__(
        self,
        api_key: str,
        source_sheet_id: str,
        target_sheet_id: str,
        source_match_column_id: int,
        target_match_column_id: int,
        temp_folder: str = "/tmp/smartsheet-attachments",
        max_retries: int = 3,
        retry_delay: float = 2.0
    ):
        """
        Initialize the AttachmentSyncer.

        Args:
            api_key: Smartsheet API key
            source_sheet_id: ID of the source sheet
            target_sheet_id: ID of the target sheet
            source_match_column_id: Column ID to match rows in source sheet
            target_match_column_id: Column ID to match rows in target sheet
            temp_folder: Temporary folder for downloading attachments
            max_retries: Maximum number of retries for transient errors
            retry_delay: Delay in seconds between retries (doubles each retry)
        """
        self.client = smartsheet.Smartsheet(api_key)
        self.client.errors_as_exceptions(True)
        self.source_sheet_id = source_sheet_id
        self.target_sheet_id = target_sheet_id
        self.source_match_column_id = source_match_column_id
        self.target_match_column_id = target_match_column_id
        self.temp_folder = temp_folder
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        # Statistics
        self.stats = {
            "total_rows_processed": 0,
            "rows_with_matches": 0,
            "rows_without_matches": 0,
            "attachments_synced": 0,
            "attachments_skipped": 0,
            "errors": 0
        }

        # Ensure temp folder exists
        os.makedirs(self.temp_folder, exist_ok=True)

    def _extract_match_key(self, value) -> Optional[int]:
        """
        Convert cell value to int key, handling decimals like "12345.0".

        Args:
            value: Cell value to convert

        Returns:
            Integer key or None if conversion fails
        """
        if value is None:
            return None
        
        try:
            # Handle both numeric and string values
            if isinstance(value, (int, float)):
                return int(value)
            elif isinstance(value, str):
                # Remove whitespace and try to convert
                cleaned = value.strip()
                if cleaned:
                    return int(float(cleaned))
            return None
        except (ValueError, TypeError):
            logger.warning(f"Could not convert value to match key: {value}")
            return None

    def _build_row_map(self, rows, match_column_id: int) -> Dict[int, object]:
        """
        Build dict mapping match keys to row objects.

        Args:
            rows: List of row objects from Smartsheet
            match_column_id: Column ID to extract match value from

        Returns:
            Dictionary mapping match keys to row objects
        """
        row_map = {}
        for row in rows:
            # Find the cell with the matching column ID
            match_value = None
            for cell in row.cells:
                if cell.column_id == match_column_id:
                    match_value = cell.value
                    break

            if match_value is not None:
                match_key = self._extract_match_key(match_value)
                if match_key is not None:
                    # Warn if duplicate keys exist
                    if match_key in row_map:
                        logger.warning(f"⚠️ Duplicate match key {match_key} found. "
                                     f"Row {row.id} will overwrite row {row_map[match_key].id}")
                    row_map[match_key] = row
                    logger.debug(f"Mapped key {match_key} to row {row.id}")

        logger.info(f"Built row map with {len(row_map)} entries")
        return row_map

    def _retry_operation(self, operation, operation_name: str, *args, **kwargs):
        """
        Retry an operation with exponential backoff for transient errors.
        
        Args:
            operation: Function to execute
            operation_name: Name of the operation for logging
            *args, **kwargs: Arguments to pass to the operation
            
        Returns:
            Result of the operation
            
        Raises:
            Exception: If all retries are exhausted
        """
        last_exception = None
        delay = self.retry_delay
        
        for attempt in range(1, self.max_retries + 1):
            try:
                return operation(*args, **kwargs)
            except (requests.exceptions.RequestException, 
                    requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError) as e:
                last_exception = e
                if attempt < self.max_retries:
                    logger.warning(f"⚠️ {operation_name} failed (attempt {attempt}/{self.max_retries}): {e}")
                    logger.info(f"   Retrying in {delay:.1f} seconds...")
                    time.sleep(delay)
                    delay *= 2  # Exponential backoff
                else:
                    logger.error(f"❌ {operation_name} failed after {self.max_retries} attempts: {e}")
                    raise
            except Exception as e:
                # For non-transient errors, don't retry
                logger.error(f"❌ {operation_name} failed with non-retryable error: {e}")
                raise
        
        # Should never reach here, but just in case
        raise last_exception

    def _download_attachment(self, name: str, url: str) -> str:
        """
        Download file to temp storage with retry logic.

        Args:
            name: Filename
            url: Download URL

        Returns:
            Path to downloaded file
        """
        # Sanitize filename to prevent path traversal
        safe_name = os.path.basename(name)
        file_path = os.path.join(self.temp_folder, safe_name)
        
        logger.debug(f"📥 Downloading attachment: {safe_name}")
        
        def _do_download():
            # Use context manager for proper resource cleanup with timeout
            with requests.get(url, stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                with open(file_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            return file_path
        
        # Use retry logic for downloads
        result = self._retry_operation(_do_download, f"Download of {safe_name}")
        logger.debug(f"✅ Downloaded to: {file_path}")
        return result

    def _build_attachment_cache(self, sheet_id: str, row_ids: List[int]) -> Dict[int, List[Any]]:
        """
        Build a cache of attachments for multiple rows to reduce API calls.
        
        Note: This only caches basic attachment metadata (without URLs) to avoid
        URL expiration issues. URLs are fetched just-in-time when needed.

        Args:
            sheet_id: Sheet ID
            row_ids: List of row IDs to fetch attachments for

        Returns:
            Dictionary mapping row_id to list of attachment metadata (without URLs)
        """
        attachment_cache = {}
        
        logger.info(f"📦 Pre-fetching attachment metadata for {len(row_ids)} rows...")
        
        for row_id in row_ids:
            try:
                attachments = self.client.Attachments.list_row_attachments(
                    sheet_id, row_id, include_all=True
                ).data
                
                # Only cache FILE type attachments (without fetching full details)
                # We intentionally don't fetch URLs here to avoid expiration issues
                file_attachments = [
                    att for att in attachments 
                    if att.attachment_type == "FILE"
                ]
                
                attachment_cache[row_id] = file_attachments
                logger.debug(f"Cached {len(file_attachments)} FILE attachment metadata for row {row_id}")
                
            except Exception as e:
                logger.warning(f"Could not get attachments for row {row_id}: {e}")
                attachment_cache[row_id] = []
        
        logger.info(f"✅ Attachment metadata cache built for {len(attachment_cache)} rows")
        return attachment_cache

    def _get_existing_attachment_names(self, sheet_id: str, row_id: int) -> Set[str]:
        """
        Get set of FILE attachment names on a row.

        Args:
            sheet_id: Sheet ID
            row_id: Row ID

        Returns:
            Set of attachment filenames
        """
        try:
            attachments = self.client.Attachments.list_row_attachments(
                sheet_id, row_id, include_all=True
            ).data

            # Only include FILE type attachments
            names = {
                att.name for att in attachments 
                if att.attachment_type == "FILE"
            }
            logger.debug(f"Found {len(names)} existing FILE attachments on row {row_id}")
            return names
        except Exception as e:
            logger.warning(f"Could not get attachments for row {row_id}: {e}")
            return set()

    def _get_attachment_names_from_cache(self, attachment_cache: Dict[int, List[Any]], row_id: int) -> Set[str]:
        """
        Extract attachment filenames from the pre-built cache for a specific row.

        Args:
            attachment_cache: Cache of attachments mapping row_id to attachment list
            row_id: Row ID to get attachment names for

        Returns:
            Set of attachment filenames
        """
        attachments = attachment_cache.get(row_id, [])
        return {att.name for att in attachments}

    def copy_attachments_to_row(
        self,
        source_sheet_id: str,
        source_row_id: int,
        target_sheet_id: str,
        target_row_id: int,
        skip_existing: bool = True,
        source_attachment_cache: Optional[Dict[int, List[Any]]] = None,
        target_attachment_cache: Optional[Dict[int, List[Any]]] = None
    ) -> int:
        """
        Copy attachments between rows.

        Args:
            source_sheet_id: Source sheet ID
            source_row_id: Source row ID
            target_sheet_id: Target sheet ID
            target_row_id: Target row ID
            skip_existing: If True, skip attachments that already exist on target
            source_attachment_cache: Optional cache of source attachments mapping row_id to attachment list
            target_attachment_cache: Optional cache of target attachments mapping row_id to attachment list

        Returns:
            Number of attachments copied
        """
        copied_count = 0

        try:
            # Get source attachments from cache or fetch directly
            if source_attachment_cache is not None:
                # Using cached metadata (without URLs to avoid expiration)
                file_attachments = source_attachment_cache.get(source_row_id, [])
            else:
                source_attachments = self.client.Attachments.list_row_attachments(
                    source_sheet_id, source_row_id, include_all=True
                ).data

                # Filter to FILE type only
                file_attachments = [
                    att for att in source_attachments 
                    if att.attachment_type == "FILE"
                ]

            if not file_attachments:
                logger.debug(f"⏭️ No FILE attachments on source row {source_row_id}")
                return 0

            # Get existing attachment names on target if skip_existing is True
            if skip_existing:
                if target_attachment_cache is not None:
                    existing_names = self._get_attachment_names_from_cache(
                        target_attachment_cache, target_row_id
                    )
                else:
                    existing_names = self._get_existing_attachment_names(
                        target_sheet_id, target_row_id
                    )

            # Process each attachment
            for attachment in file_attachments:
                file_path = None  # Initialize to track cleanup
                try:
                    # Skip if already exists
                    if skip_existing and attachment.name in existing_names:
                        logger.info(f"⏭️ Skipping duplicate: {attachment.name}")
                        self.stats["attachments_skipped"] += 1
                        continue

                    # Fetch fresh URL just before downloading to avoid expiration issues
                    # This applies to both cached and non-cached paths to ensure URLs are always fresh
                    try:
                        def _get_attachment():
                            return self.client.Attachments.get_attachment(
                                source_sheet_id, attachment.id
                            )
                        
                        full_attachment = self._retry_operation(
                            _get_attachment,
                            f"Get attachment details for '{attachment.name}'"
                        )
                        download_url = full_attachment.url
                        mime_type = full_attachment.mime_type
                    except Exception as e:
                        logger.warning(f"⚠️ Could not get fresh URL for attachment '{attachment.name}' "
                                     f"(ID: {attachment.id}, Sheet: {source_sheet_id}): {e}")
                        self.stats["attachments_skipped"] += 1
                        continue

                    # Validate attachment URL
                    if not download_url or not str(download_url).strip():
                        logger.warning(f"⚠️ Skipping attachment '{attachment.name}' due to missing or invalid URL")
                        self.stats["attachments_skipped"] += 1
                        continue

                    # Download attachment (with built-in retry)
                    file_path = self._download_attachment(
                        attachment.name, 
                        download_url
                    )

                    # Upload to target row (with retry)
                    logger.info(f"📤 Uploading {attachment.name} to target row {target_row_id}")
                    
                    def _upload_attachment():
                        with open(file_path, 'rb') as f:
                            return self.client.Attachments.attach_file_to_row(
                                target_sheet_id,
                                target_row_id,
                                (attachment.name, f, mime_type)
                            )
                    
                    self._retry_operation(
                        _upload_attachment,
                        f"Upload of {attachment.name}"
                    )

                    logger.info(f"✅ Successfully copied: {attachment.name}")
                    copied_count += 1
                    self.stats["attachments_synced"] += 1

                except Exception as e:
                    logger.error(f"❌ Error copying attachment {attachment.name}: {e}")
                    self.stats["errors"] += 1
                finally:
                    # Clean up downloaded file
                    if file_path and os.path.exists(file_path):
                        try:
                            os.remove(file_path)
                            logger.debug(f"🗑️ Cleaned up temp file: {file_path}")
                        except Exception as e:
                            logger.warning(f"Could not delete temp file {file_path}: {e}")

        except Exception as e:
            logger.error(f"❌ Error processing attachments for row {source_row_id}: {e}")
            self.stats["errors"] += 1

        return copied_count

    def sync_attachments(self) -> Dict[str, int]:
        """
        Main sync method: source → target.

        Returns:
            Dictionary with sync statistics
        """
        logger.info("=" * 60)
        logger.info("🚀 Starting Smartsheet Attachment Sync")
        logger.info("=" * 60)

        try:
            # Load sheets
            logger.info(f"📋 Loading source sheet: {self.source_sheet_id}")
            source_sheet = self.client.Sheets.get_sheet(
                self.source_sheet_id
            )

            logger.info(f"📋 Loading target sheet: {self.target_sheet_id}")
            target_sheet = self.client.Sheets.get_sheet(
                self.target_sheet_id
            )

            # Build row maps
            logger.info(f"🗺️ Building row maps using match columns...")
            source_map = self._build_row_map(
                source_sheet.rows, 
                self.source_match_column_id
            )
            target_map = self._build_row_map(
                target_sheet.rows, 
                self.target_match_column_id
            )

            logger.info(f"📊 Source rows with match values: {len(source_map)}")
            logger.info(f"📊 Target rows with match values: {len(target_map)}")

            # Identify matched row IDs to pre-fetch attachments
            matched_source_row_ids = []
            matched_target_row_ids = []
            
            for match_key, source_row in source_map.items():
                if match_key in target_map:
                    matched_source_row_ids.append(source_row.id)
                    matched_target_row_ids.append(target_map[match_key].id)
            
            # Pre-fetch attachments for all matched rows to reduce N+1 API calls
            source_attachment_cache = self._build_attachment_cache(
                self.source_sheet_id, matched_source_row_ids
            )
            target_attachment_cache = self._build_attachment_cache(
                self.target_sheet_id, matched_target_row_ids
            )

            # Sync from source to target
            logger.info("🔄 Starting attachment sync...")
            for match_key, source_row in source_map.items():
                self.stats["total_rows_processed"] += 1

                if match_key not in target_map:
                    logger.info(f"⏭️ No matching target row for key {match_key}, skipping")
                    self.stats["rows_without_matches"] += 1
                    continue

                target_row = target_map[match_key]
                self.stats["rows_with_matches"] += 1

                logger.info(f"🔗 Processing match key {match_key}: "
                          f"source row {source_row.id} → target row {target_row.id}")

                # Copy new attachments from source to target using cached data
                copied = self.copy_attachments_to_row(
                    source_sheet_id=self.source_sheet_id,
                    source_row_id=source_row.id,
                    target_sheet_id=self.target_sheet_id,
                    target_row_id=target_row.id,
                    skip_existing=True,  # Prevents duplicates
                    source_attachment_cache=source_attachment_cache,
                    target_attachment_cache=target_attachment_cache
                )

                if copied > 0:
                    logger.info(f"✅ Copied {copied} attachment(s) for match key {match_key}")

            # Print summary
            logger.info("=" * 60)
            logger.info("📊 SYNC SUMMARY")
            logger.info("=" * 60)
            logger.info(f"Total rows processed: {self.stats['total_rows_processed']}")
            logger.info(f"Rows with matches: {self.stats['rows_with_matches']}")
            logger.info(f"Rows without matches: {self.stats['rows_without_matches']}")
            logger.info(f"Attachments synced: {self.stats['attachments_synced']}")
            logger.info(f"Attachments skipped (duplicates): {self.stats['attachments_skipped']}")
            logger.info(f"Errors: {self.stats['errors']}")
            logger.info("=" * 60)

            if self.stats["errors"] > 0:
                logger.warning(f"⚠️ Sync completed with {self.stats['errors']} error(s)")
            else:
                logger.info("✅ Sync completed successfully!")

        except Exception as e:
            logger.error(f"❌ Fatal error during sync: {e}")
            self.stats["errors"] += 1
            raise

        return self.stats
