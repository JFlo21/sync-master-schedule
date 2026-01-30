# Sync Master Schedule - Smartsheet Attachment Sync

A Python application that automatically syncs FILE-type attachments between Smartsheet sheets. The app matches rows based on column name and copies attachments from source to target rows, intelligently skipping duplicates.

## Features

- ✅ Syncs FILE-type attachments between Smartsheet sheets
- ✅ Matches rows by configurable column name
- ✅ Skips duplicate attachments automatically
- ✅ Runs daily at 5:00 AM UTC via GitHub Actions
- ✅ Comprehensive logging for monitoring and debugging
- ✅ Environment-based configuration

## Prerequisites

- Python 3.11 or higher
- Smartsheet API access token
- Source and target Smartsheet sheet IDs
- Column name that exists in both sheets for row matching

## Setup

### 1. Clone the Repository

```bash
git clone https://github.com/JFlo21/sync-master-schedule.git
cd sync-master-schedule
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

The application requires the following environment variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `SMARTSHEET_ACCESS_TOKEN` | Your Smartsheet API access token | `abc123xyz...` |
| `SOURCE_SHEET_ID` | ID of the sheet to copy attachments from | `1234567890123456` |
| `TARGET_SHEET_ID` | ID of the sheet to copy attachments to | `9876543210987654` |
| `MATCH_COLUMN_NAME` | Column name used to match rows between sheets | `Project ID` |

#### Getting Your Smartsheet Access Token

1. Log in to your Smartsheet account
2. Click on your profile picture in the top right
3. Select "Apps & Integrations"
4. Click "API Access"
5. Generate a new access token

#### Finding Sheet IDs

Sheet IDs can be found in the URL when viewing a sheet:
```
https://app.smartsheet.com/sheets/[SHEET_ID]
```

### 4. Configure GitHub Secrets and Variables

For the GitHub Actions workflow to run automatically:

1. Go to your repository Settings → Secrets and variables → Actions
2. Add the following **repository secrets**:
   - `SMARTSHEET_ACCESS_TOKEN`
   - `SOURCE_SHEET_ID`
   - `TARGET_SHEET_ID`
3. Edit the workflow file `.github/workflows/sync-attachments.yml` to set the `MATCH_COLUMN_NAME` in the `env:` section (line 15) to match your column name.

## Usage

### Run Locally

Set environment variables and run the script:

```bash
export SMARTSHEET_ACCESS_TOKEN="your-token"
export SOURCE_SHEET_ID="source-sheet-id"
export TARGET_SHEET_ID="target-sheet-id"
export MATCH_COLUMN_NAME="Project ID"

python sync_attachments.py
```

Or use a `.env` file (not committed to git):

```bash
# .env file
SMARTSHEET_ACCESS_TOKEN=your-token
SOURCE_SHEET_ID=source-sheet-id
TARGET_SHEET_ID=target-sheet-id
MATCH_COLUMN_NAME=Project ID
```

### Automated Execution via GitHub Actions

The sync runs automatically every day at 5:00 AM UTC. You can also:

1. **Manually trigger** the workflow:
   - Go to Actions tab in your GitHub repository
   - Select "Sync Smartsheet Attachments"
   - Click "Run workflow"

2. **Monitor execution**:
   - Check the Actions tab for workflow runs
   - Review logs for sync statistics and any errors

## How It Works

1. **Fetch Sheets**: Retrieves both source and target sheets from Smartsheet
2. **Match Rows**: Builds a mapping of rows between sheets using the specified column
3. **Get Attachments**: Retrieves FILE-type attachments from matched source rows
4. **Check Duplicates**: Compares attachment names to skip existing files
5. **Copy Attachments**: Downloads from source and uploads to target (only new files)
6. **Report Results**: Logs statistics about the sync operation

## Sync Statistics

The script logs the following statistics:
- `matched_rows`: Number of rows matched between sheets
- `attachments_copied`: Number of attachments successfully copied
- `attachments_skipped`: Number of duplicate attachments skipped
- `errors`: Number of errors encountered

## Troubleshooting

### Common Issues

**Missing environment variables**
- Error: `Missing required environment variables`
- Solution: Ensure all four required environment variables are set

**Column not found**
- Error: `Column 'X' not found in source/target sheet`
- Solution: Verify the column name exists in both sheets with exact spelling

**Authentication errors**
- Error: Authentication or API access errors
- Solution: Verify your Smartsheet access token is valid and has necessary permissions

**Rate limiting**
- Solution: The Smartsheet SDK handles rate limiting automatically

## Development

### Project Structure

```
.
├── .github/
│   └── workflows/
│       └── sync-attachments.yml    # GitHub Actions workflow
├── .gitignore                      # Python gitignore
├── README.md                       # This file
├── requirements.txt                # Python dependencies
└── sync_attachments.py             # Main sync script
```

### Dependencies

- `smartsheet-python-sdk>=2.105.0` - Official Smartsheet Python SDK
- `python-dotenv>=1.0.0` - Environment variable management

## Security Notes

- Never commit your Smartsheet access token or sheet IDs to version control
- Use GitHub Secrets for sensitive data in workflows
- Access tokens should be treated as passwords
- Regularly rotate your API access tokens

## License

This project is provided as-is for internal use.

## Support

For issues or questions:
1. Check the troubleshooting section above
2. Review the GitHub Actions logs for detailed error messages
3. Consult the [Smartsheet API documentation](https://smartsheet.redoc.ly/)