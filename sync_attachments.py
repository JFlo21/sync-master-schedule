#!/usr/bin/env python3
"""
Smartsheet Attachment Sync Script

This script syncs FILE-type attachments between Smartsheet sheets by matching rows
based on column ID criteria. It skips duplicate attachments that already exist.

Environment Variables Required:
    SMARTSHEET_ACCESS_TOKEN: API access token for Smartsheet
    SOURCE_SHEET_ID: ID of the source sheet to copy attachments from
    TARGET_SHEET_ID: ID of the target sheet to copy attachments to
    MATCH_COLUMN_NAME: Name of the column to use for matching rows between sheets
"""

import os
import sys
import logging
from typing import Dict, List, Optional, Set
import smartsheet

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


class SmartsheetAttachmentSync:
    """Handles syncing of attachments between Smartsheet sheets."""
    
    def __init__(self, access_token: str, source_sheet_id: str, target_sheet_id: str, match_column_name: str):
        """
        Initialize the attachment sync handler.
        
        Args:
            access_token: Smartsheet API access token
            source_sheet_id: ID of the source sheet
            target_sheet_id: ID of the target sheet
            match_column_name: Name of the column to match rows by
        """
        self.client = smartsheet.Smartsheet(access_token)
        self.client.errors_as_exceptions(True)
        self.source_sheet_id = source_sheet_id
        self.target_sheet_id = target_sheet_id
        self.match_column_name = match_column_name
        
    def get_sheet(self, sheet_id: str):
        """
        Fetch a sheet with all its data.
        
        Args:
            sheet_id: ID of the sheet to fetch
            
        Returns:
            Sheet object
        """
        logger.info(f"Fetching sheet {sheet_id}")
        return self.client.Sheets.get_sheet(sheet_id)
    
    def get_column_id_by_name(self, sheet, column_name: str) -> Optional[int]:
        """
        Get the column ID by column name.
        
        Args:
            sheet: Sheet object
            column_name: Name of the column to find
            
        Returns:
            Column ID if found, None otherwise
        """
        for column in sheet.columns:
            if column.title == column_name:
                return column.id
        return None
    
    def build_row_map(self, sheet, column_id: int) -> Dict[str, int]:
        """
        Build a mapping of column values to row IDs.
        
        Args:
            sheet: Sheet object
            column_id: ID of the column to use for mapping
            
        Returns:
            Dictionary mapping column values to row IDs
        """
        row_map = {}
        for row in sheet.rows:
            for cell in row.cells:
                if cell.column_id == column_id and cell.value:
                    row_map[str(cell.value)] = row.id
                    break
        return row_map
    
    def get_row_attachments(self, sheet_id: str, row_id: int) -> List:
        """
        Get all attachments for a specific row.
        
        Args:
            sheet_id: ID of the sheet
            row_id: ID of the row
            
        Returns:
            List of attachment objects
        """
        try:
            response = self.client.Attachments.list_row_attachments(sheet_id, row_id, include_all=True)
            # Filter for FILE type attachments only
            file_attachments = [att for att in response.data if att.attachment_type == 'FILE']
            return file_attachments
        except Exception as e:
            logger.error(f"Error fetching attachments for row {row_id}: {e}")
            return []
    
    def get_existing_attachment_names(self, sheet_id: str, row_id: int) -> Set[str]:
        """
        Get the names of all existing attachments on a row.
        
        Args:
            sheet_id: ID of the sheet
            row_id: ID of the row
            
        Returns:
            Set of attachment names
        """
        attachments = self.get_row_attachments(sheet_id, row_id)
        return {att.name for att in attachments}
    
    def attach_file_to_row(self, sheet_id: str, row_id: int, attachment) -> bool:
        """
        Attach a file from one row to another row.
        
        Args:
            sheet_id: Target sheet ID
            row_id: Target row ID
            attachment: Source attachment object
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Download the attachment from source
            file_content = self.client.Attachments.get_attachment(
                self.source_sheet_id,
                attachment.id
            )
            
            # Upload to target row
            self.client.Attachments.attach_file_to_row(
                sheet_id,
                row_id,
                (attachment.name, file_content, attachment.mime_type)
            )
            logger.info(f"Successfully attached '{attachment.name}' to row {row_id}")
            return True
        except Exception as e:
            logger.error(f"Error attaching file '{attachment.name}' to row {row_id}: {e}")
            return False
    
    def sync_attachments(self) -> Dict[str, int]:
        """
        Sync attachments from source sheet to target sheet.
        
        Returns:
            Dictionary with sync statistics
        """
        stats = {
            'matched_rows': 0,
            'attachments_copied': 0,
            'attachments_skipped': 0,
            'errors': 0
        }
        
        try:
            # Fetch both sheets
            logger.info("Starting attachment sync process")
            source_sheet = self.get_sheet(self.source_sheet_id)
            target_sheet = self.get_sheet(self.target_sheet_id)
            
            # Get column IDs for matching
            source_column_id = self.get_column_id_by_name(source_sheet, self.match_column_name)
            target_column_id = self.get_column_id_by_name(target_sheet, self.match_column_name)
            
            if not source_column_id:
                logger.error(f"Column '{self.match_column_name}' not found in source sheet")
                return stats
            
            if not target_column_id:
                logger.error(f"Column '{self.match_column_name}' not found in target sheet")
                return stats
            
            # Build row mappings
            logger.info(f"Building row mappings based on column '{self.match_column_name}'")
            source_row_map = self.build_row_map(source_sheet, source_column_id)
            target_row_map = self.build_row_map(target_sheet, target_column_id)
            
            logger.info(f"Found {len(source_row_map)} rows in source, {len(target_row_map)} rows in target")
            
            # Process each matching row
            for match_value, source_row_id in source_row_map.items():
                if match_value not in target_row_map:
                    logger.debug(f"No matching row found for value '{match_value}'")
                    continue
                
                target_row_id = target_row_map[match_value]
                stats['matched_rows'] += 1
                
                # Get source attachments
                source_attachments = self.get_row_attachments(self.source_sheet_id, source_row_id)
                if not source_attachments:
                    logger.debug(f"No FILE attachments found for match value '{match_value}'")
                    continue
                
                # Get existing target attachments
                existing_attachments = self.get_existing_attachment_names(self.target_sheet_id, target_row_id)
                
                # Copy attachments that don't already exist
                for attachment in source_attachments:
                    if attachment.name in existing_attachments:
                        logger.info(f"Skipping duplicate attachment '{attachment.name}' for match value '{match_value}'")
                        stats['attachments_skipped'] += 1
                    else:
                        logger.info(f"Copying attachment '{attachment.name}' for match value '{match_value}'")
                        if self.attach_file_to_row(self.target_sheet_id, target_row_id, attachment):
                            stats['attachments_copied'] += 1
                        else:
                            stats['errors'] += 1
            
            logger.info(f"Sync completed. Stats: {stats}")
            return stats
            
        except Exception as e:
            logger.error(f"Error during sync process: {e}")
            stats['errors'] += 1
            return stats


def main():
    """Main entry point for the script."""
    # Load environment variables
    access_token = os.environ.get('SMARTSHEET_ACCESS_TOKEN')
    source_sheet_id = os.environ.get('SOURCE_SHEET_ID')
    target_sheet_id = os.environ.get('TARGET_SHEET_ID')
    match_column_name = os.environ.get('MATCH_COLUMN_NAME')
    
    # Validate required environment variables
    missing_vars = []
    if not access_token:
        missing_vars.append('SMARTSHEET_ACCESS_TOKEN')
    if not source_sheet_id:
        missing_vars.append('SOURCE_SHEET_ID')
    if not target_sheet_id:
        missing_vars.append('TARGET_SHEET_ID')
    if not match_column_name:
        missing_vars.append('MATCH_COLUMN_NAME')
    
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        sys.exit(1)
    
    # Run the sync
    try:
        syncer = SmartsheetAttachmentSync(
            access_token=access_token,
            source_sheet_id=source_sheet_id,
            target_sheet_id=target_sheet_id,
            match_column_name=match_column_name
        )
        
        stats = syncer.sync_attachments()
        
        # Exit with error code if there were errors
        if stats['errors'] > 0:
            logger.error(f"Sync completed with {stats['errors']} errors")
            sys.exit(1)
        else:
            logger.info("Sync completed successfully")
            sys.exit(0)
            
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
