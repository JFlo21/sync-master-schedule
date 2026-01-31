# Smartsheet Attachment Sync

Automatically synchronize attachments between two Smartsheet sheets based on matching column criteria. Runs daily at 5:00 AM UTC via GitHub Actions.

## Overview

This application copies attachments from a **source sheet** to a **target sheet** by matching rows using a specified column value (e.g., WR Number). It prevents duplicate attachments and provides comprehensive logging with emoji indicators for easy monitoring.

### Sync Flow

```
SOURCE SHEET (1553121697288068) → TARGET SHEET (4850043816202116)
├── Match Column: Source ID 8022925527175044 → Target ID 1437881091182468
├── Process: Find matching rows
├── Copy: New FILE attachments only
└── Skip: Duplicates and non-matching rows
```

## Features

- ✅ **Automatic Daily Sync** - Runs at 5:00 AM UTC via GitHub Actions
- 🔄 **Smart Row Matching** - Matches rows using configurable column IDs
- 📎 **File Attachment Only** - Syncs only FILE-type attachments, skips links
- 🛡️ **Duplicate Prevention** - Skips attachments that already exist on target
- 🔢 **Decimal Key Handling** - Properly matches values like "12345.0" with "12345"
- 🧹 **Auto Cleanup** - Removes temporary files after processing
- 📊 **Detailed Logging** - Comprehensive logs with emoji status indicators
- 📈 **Statistics Tracking** - Reports synced, skipped, and error counts

## Prerequisites

- Python 3.11 or higher
- Smartsheet API key with access to both source and target sheets
- GitHub repository (for automated runs via GitHub Actions)

## Local Development Setup

### 1. Clone the Repository

```bash
git clone https://github.com/JFlo21/sync-master-schedule.git
cd sync-master-schedule
```

### 2. Create Virtual Environment

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env` with your actual values:

```
SMARTSHEET_API_KEY=your_api_key_here
SOURCE_SHEET_ID=1553121697288068
TARGET_SHEET_ID=4850043816202116
SOURCE_MATCH_COLUMN_ID=8022925527175044
TARGET_MATCH_COLUMN_ID=1437881091182468
TEMP_DOWNLOAD_FOLDER=/tmp/smartsheet-attachments
```

### 5. Run the Sync

```bash
python main.py
```

## GitHub Actions Setup

### Required Secrets

Navigate to your repository's **Settings → Secrets and variables → Actions** and add the following secrets:

| Secret Name | Required | Default Value | Description |
|-------------|----------|---------------|-------------|
| `SMARTSHEET_API_KEY` | ✅ Yes | None | Your Smartsheet API key |
| `SOURCE_SHEET_ID` | No | 1553121697288068 | Source sheet ID |
| `TARGET_SHEET_ID` | No | 4850043816202116 | Target sheet ID |
| `SOURCE_MATCH_COLUMN_ID` | No | 8022925527175044 | Source column ID for matching |
| `TARGET_MATCH_COLUMN_ID` | No | 1437881091182468 | Target column ID for matching |

### Workflow Schedule

The sync runs automatically:
- **Daily at 5:00 AM UTC** via cron schedule
- **Manual trigger** via workflow_dispatch (GitHub Actions UI)

To manually trigger:
1. Go to **Actions** tab in your repository
2. Select **Smartsheet Attachment Sync** workflow
3. Click **Run workflow**

## How It Works

### Sync Logic

1. **Load Sheets** - Fetches both source and target sheets with attachment metadata
2. **Build Row Maps** - Creates mappings using the match column (e.g., WR Number)
3. **Process Each Source Row**:
   - Find matching target row by match column value
   - If match found: Copy NEW FILE attachments (skip duplicates by filename)
   - If no match found: Skip row with log message
   - If no new attachments: Skip row
4. **Log Results** - Reports comprehensive statistics and status

### Key Features

#### Duplicate Prevention
The syncer checks existing attachment filenames on the target row before uploading. If an attachment with the same name already exists, it's skipped to prevent duplicates.

#### Decimal Key Matching
Values like "12345.0" are normalized to integer "12345" for reliable matching across both sheets.

#### FILE Attachments Only
Only FILE-type attachments are synced. Link attachments and other types are automatically excluded.

#### Error Resilience
If one attachment fails to copy, the sync continues processing other attachments and rows. All errors are logged and counted in the final statistics.

## Project Structure

```
sync-master-schedule/
├── .github/
│   └── workflows/
│       └── sync-attachments.yml     # GitHub Actions workflow
├── src/
│   ├── __init__.py                  # Package initialization
│   ├── config.py                    # Configuration management
│   └── attachment_sync.py           # Core sync logic
├── .env.example                     # Environment variables template
├── .gitignore                       # Python gitignore
├── requirements.txt                 # Python dependencies
├── README.md                        # This file
└── main.py                          # Application entry point
```

## Configuration Reference

### Environment Variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `SMARTSHEET_API_KEY` | string | Required | Smartsheet API key |
| `SOURCE_SHEET_ID` | string | 1553121697288068 | Source sheet ID |
| `TARGET_SHEET_ID` | string | 4850043816202116 | Target sheet ID |
| `SOURCE_MATCH_COLUMN_ID` | int | 8022925527175044 | Column ID for matching in source |
| `TARGET_MATCH_COLUMN_ID` | int | 1437881091182468 | Column ID for matching in target |
| `TEMP_DOWNLOAD_FOLDER` | string | /tmp/smartsheet-attachments | Temporary download location |

### Finding Sheet and Column IDs

#### Sheet ID
- Open your sheet in Smartsheet
- Look at the URL: `https://app.smartsheet.com/sheets/XXXXXX`
- The `XXXXXX` is your sheet ID

#### Column ID
Use the Smartsheet API to get column information:

```bash
curl https://api.smartsheet.com/2.0/sheets/{sheetId} \
  -H "Authorization: Bearer YOUR_API_KEY"
```

Look for the column name in the response and note its `id` field.

## Logging and Monitoring

### Log Emoji Indicators

- 🚀 - Sync start
- 📋 - Loading sheet
- 🗺️ - Building row maps
- 🔄 - Starting sync
- 🔗 - Processing row match
- 📥 - Downloading attachment
- 📤 - Uploading attachment
- ✅ - Success
- ⏭️ - Skipped (no match or duplicate)
- ❌ - Error
- 🗑️ - Cleanup
- ⚠️ - Warning
- 📊 - Statistics

### Example Log Output

```
2024-01-30 05:00:15 - INFO - ============================================================
2024-01-30 05:00:15 - INFO - 🚀 Starting Smartsheet Attachment Sync
2024-01-30 05:00:15 - INFO - ============================================================
2024-01-30 05:00:16 - INFO - 📋 Loading source sheet: 1553121697288068
2024-01-30 05:00:17 - INFO - 📋 Loading target sheet: 4850043816202116
2024-01-30 05:00:18 - INFO - 🗺️ Building row maps using match columns...
2024-01-30 05:00:18 - INFO - 📊 Source rows with match values: 150
2024-01-30 05:00:18 - INFO - 📊 Target rows with match values: 145
2024-01-30 05:00:18 - INFO - 🔄 Starting attachment sync...
2024-01-30 05:00:19 - INFO - 🔗 Processing match key 12345: source row 123 → target row 456
2024-01-30 05:00:20 - INFO - 📤 Uploading document.pdf to target row 456
2024-01-30 05:00:21 - INFO - ✅ Successfully copied: document.pdf
2024-01-30 05:00:22 - INFO - ⏭️ Skipping duplicate: spreadsheet.xlsx
2024-01-30 05:00:23 - INFO - ============================================================
2024-01-30 05:00:23 - INFO - 📊 SYNC SUMMARY
2024-01-30 05:00:23 - INFO - ============================================================
2024-01-30 05:00:23 - INFO - Total rows processed: 150
2024-01-30 05:00:23 - INFO - Rows with matches: 145
2024-01-30 05:00:23 - INFO - Rows without matches: 5
2024-01-30 05:00:23 - INFO - Attachments synced: 42
2024-01-30 05:00:23 - INFO - Attachments skipped (duplicates): 18
2024-01-30 05:00:23 - INFO - Errors: 0
2024-01-30 05:00:23 - INFO - ============================================================
2024-01-30 05:00:23 - INFO - ✅ Sync completed successfully!
```

## Troubleshooting

### Common Issues

#### "SMARTSHEET_API_KEY is required but not set"

**Solution:** Ensure your API key is set in the `.env` file (local) or GitHub Secrets (GitHub Actions).

```bash
# Local - Add to .env file
SMARTSHEET_API_KEY=your_actual_api_key

# GitHub Actions - Add to repository secrets
# Settings → Secrets and variables → Actions → New repository secret
```

#### "No matching target row for key XXXXX"

**Explanation:** This is normal and means a source row doesn't have a corresponding row in the target sheet with the same match column value. The row is skipped.

#### "Could not convert value to match key"

**Solution:** Check that your match column contains numeric values. The syncer expects integer values or values that can be converted to integers (like "12345.0").

#### Permission Denied Errors

**Solution:** Ensure your Smartsheet API key has:
- Read access to the source sheet
- Write access to the target sheet
- Permission to read and create attachments

#### Attachment Download Failures

**Solution:** 
- Check your internet connection
- Verify the source sheet's attachments are not corrupted
- Ensure sufficient disk space in the temp folder

### Debug Mode

For more detailed logging, you can modify `main.py` to set the log level to `DEBUG`:

```python
logging.basicConfig(
    level=logging.DEBUG,  # Changed from INFO
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
```

## Security Notes

- ⚠️ **Never commit your `.env` file** - It contains sensitive API keys
- ⚠️ **Use GitHub Secrets** for production credentials
- ⚠️ **Rotate API keys regularly** for security
- ⚠️ **Grant minimum required permissions** to the API key

## Dependencies

- `smartsheet-python-sdk>=3.0.0` - Official Smartsheet Python SDK
- `requests>=2.28.0` - HTTP library for downloading attachments
- `python-dotenv>=1.0.0` - Load environment variables from .env file

## License

This project is provided as-is for synchronizing Smartsheet attachments.

## Support

For issues or questions:
1. Check the [Troubleshooting](#troubleshooting) section
2. Review the logs for specific error messages
3. Verify your configuration and API key permissions
4. Open an issue in the GitHub repository

## Contributing

Contributions are welcome! Please:
1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

---

**Note:** This tool is designed to sync attachments from source to target. It does not perform bidirectional sync or delete attachments from either sheet.