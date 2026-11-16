[![Smartsheet Attachment Sync](https://github.com/JFlo21/sync-master-schedule/actions/workflows/sync-attachments.yml/badge.svg)](https://github.com/JFlo21/sync-master-schedule/actions/workflows/sync-attachments.yml)

# Smartsheet Attachment Sync

Copies PDF file attachments from **Project List** rows to the matching, already-existing rows of **Project List - Blank Copy**, matched by `Work Request #`. Runs daily at 5:00 AM UTC via GitHub Actions.

## Overview

This is an **attachment-only** sync. It reads the source sheet, finds the target row with the same `Work Request #`, and attaches the source row's PDF files to that target row. It never creates, updates, moves, or deletes rows or cells, and never deletes or replaces attachments. The only Smartsheet write it performs is attaching a PDF to an existing, verified target row.

### Sync Flow

```
Project List (1553121697288068) → Project List - Blank Copy (573385022328708)
├── Match: "Work Request #" on both sheets (column 8022925527175044 → column 7068369005875076)
├── Preflight: sheet IDs, column IDs and column titles verified before any write
├── Copy: PDF FILE attachments only (content checked for a %PDF- signature)
└── Skip: existing filenames, unmatched/invalid/ambiguous WRs, non-PDF files
```

The old target sheet (4850043816202116, match column 1437881091182468) is retired; the job refuses to run if configured to upload there.

## Features

- ✅ **Automatic Daily Sync** - Runs at 5:00 AM UTC via GitHub Actions with `--apply`
- 🧪 **Dry Run by Default** - `python main.py` plans only; uploads need `--apply`
- 🔒 **Pinned Routing** - Source/target sheet and column IDs are verified in code and checked against the live sheets before any write
- 🔢 **Exact WR Matching** - `92873293`, `92873293.0`, `"92873293"` and `" 92873293.0 "` match; placeholders such as `WR-01` or `CANCELED` are skipped
- 📄 **PDF Files Only** - Real PDF files (MIME + `%PDF-` signature); Excel, images, links and cloud shortcuts are excluded
- 🛡️ **Filename Duplicate Prevention** - Skips a PDF whose filename already exists on the target row
- 🔁 **Safe Retries** - Uploads are never blindly retried; an ambiguous upload is reconciled by re-listing the target row
- 🧹 **Auto Cleanup** - Removes temporary files after processing
- 📈 **Statistics and Truthful Exit Status** - Every count is reported; unresolved errors exit nonzero

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
TARGET_SHEET_ID=573385022328708
SOURCE_MATCH_COLUMN_ID=8022925527175044
TARGET_MATCH_COLUMN_ID=7068369005875076
TEMP_DOWNLOAD_FOLDER=/tmp/smartsheet-attachments
```

The routing IDs are optional; if set they must equal the verified values pinned in `src/config.py`, otherwise the job refuses to run. Retargeting is a reviewed code change, not a config override.

### 5. Run the Sync

```bash
python main.py --dry-run   # plan only: matching + PDF planning, no downloads, no uploads
python main.py --apply     # download, verify and upload PDFs to the target sheet
python main.py             # same as --dry-run (safe default)
```

A dry run still reads both sheets, the source attachment listings, and the attachment listings of target rows that have PDF candidates (to compare filenames), but it never downloads a file and never writes to Smartsheet.

Inside GitHub Actions (`GITHUB_ACTIONS=true`) a mode flag is required: `python main.py` with no flag exits 1 before any API call, so a workflow that forgets the flag fails loudly instead of running a silent dry run. Local runs keep the dry-run default. "Planned" PDFs in a dry run are metadata candidates; only `--apply` checks file content.

## GitHub Actions Setup

### Required Secrets

Navigate to your repository's **Settings → Secrets and variables → Actions**:

| Secret Name | Required | Description |
|-------------|----------|-------------|
| `SMARTSHEET_API_KEY` | ✅ Yes | Your Smartsheet API key (the only secret) |

The four routing IDs are **not** secrets. They are written as literal values in `.github/workflows/sync-attachments.yml`, so any old `SOURCE_SHEET_ID`, `TARGET_SHEET_ID`, `SOURCE_MATCH_COLUMN_ID` or `TARGET_MATCH_COLUMN_ID` repository secrets are ignored and cannot route the job to the old target. They can be deleted.

### Workflow Schedule

The sync runs:
- **Daily at 5:00 AM UTC** via cron schedule, with `--apply`
- **Manual trigger** via workflow_dispatch, with a **mode** choice: `dry-run` (default) or `apply`

To manually trigger:
1. Go to **Actions** tab in your repository
2. Select **Smartsheet Attachment Sync** workflow
3. Click **Run workflow**, choose the mode (`dry-run` unless you intend to upload), and run

Only one run at a time can work on the target: scheduled and manual runs share a concurrency group (`smartsheet-attachment-sync-target-573385022328708`, `cancel-in-progress: false`). A new run waits; an active run is never cancelled mid-upload. A local `python main.py --apply` is **not** covered by this concurrency group; do not run it while a workflow run is active.

## How It Works

### Sync Logic

1. **Load and verify sheets** - Loads both sheets and checks, before any write, that each sheet ID is the configured one, that the configured match column exists and is titled `Work Request #`, and that every row was loaded. Any mismatch stops the run with exit status 1.
2. **Index Work Request #** - Builds an exact key for each row (see matching rules).
3. **Match** - For each valid source WR: skip it if it appears more than once in either sheet (ambiguous, row IDs reported), skip it if the target has no such row (rows are never created), otherwise pair the source row with the target row ID returned by the API.
4. **Plan PDFs** - Lists each matched source row's FILE attachments and keeps only PDF candidates.
5. **Copy (apply only)** - Lists the target row's attachments just in time, then for each new filename downloads the PDF with a fresh URL, checks the `%PDF-` signature, and uploads the file bytes with the original filename and MIME type `application/pdf`.
6. **Report** - Logs every counter and sets the exit status.

### Work Request # Matching

- Integer-like values match exactly: `92873293`, `92873293.0`, `"92873293"` and `" 92873293.0 "` are the same WR. Text is parsed exactly (ASCII digits, optional `.0`), never through a float conversion, and characters are never stripped to manufacture a match.
- Blanks, booleans, fractions (`1.5`), non-finite numbers, and placeholders (`WR-01`, `WR-02`, `CANCELED`, `Canceled`, ...) are skipped and counted, never matched to each other.
- Matching never uses the displayed Row ID, Scope #, row position, filenames, or helper formulas.

### PDF Rules

- Only FILE attachments of the matched source row are considered, including files on that row's discussions (parent type `ROW` or `COMMENT`). Links, cloud-storage shortcuts, comment text, sheet-level attachments and proofs are never copied; files with another parent are skipped with a warning.
- MIME type `application/pdf` is a PDF candidate. When the MIME type is missing or generic (`application/octet-stream`), a case-insensitive `.pdf` extension is accepted as a fallback. A `.pdf` name with a different MIME type is a conflict and is skipped with a warning.
- "PDF" means an actual PDF file, not a filename containing "Adobe" or a file produced by a particular application. Nothing is converted to PDF.
- Before upload, the downloaded file must contain the `%PDF-` signature within its first 1024 bytes (where PDF readers look for it). HTML error pages and renamed non-PDFs are rejected and counted as errors.
- A PDF whose filename contains a double quote or a control character is skipped with a warning: the upload would store it under a percent-encoded name, so the filename duplicate check could never recognise it and every run would upload it again. If the server ever reports storing a file under a different name, the run reports an error.
- The same rules apply whether source metadata came from the pre-fetched cache or a direct listing.

### Duplicate Policy (filename-based)

A PDF is skipped when a FILE attachment with the same filename (exact, case-sensitive) already exists on the target row. This is **filename-based**, not content or version synchronization: a changed PDF re-uploaded under the same name in the source is not copied again, and existing target attachments are never replaced or deleted. A filename repeated on the same source row is copied once and the collision is reported. Once an upload has been sent for a filename, a same-named file is not sent to that row again in the same run, even if the first upload was rejected or could not be confirmed. After a confirmed upload the filename is recorded immediately, so a repeated run adds nothing.

### Failure Handling

- **Listings:** attachment listings are complete (`include_all`) and retried with bounded backoff. A listing that fails or comes back incomplete makes the row's state *unknown*: nothing is written to that row and an error is counted. It is never treated as empty.
- **Downloads:** bounded attempts, each with a fresh download URL. A body shorter than the declared `Content-Length` counts as a failed attempt. Download URLs are signed and are never logged.
- **Uploads:** at most one upload starts every 2.5 seconds (Smartsheet allows 30 attachment uploads per minute). A rate-limit rejection (nothing stored) waits and retries. A definite rejection (e.g. permissions) is not retried. An ambiguous outcome (timeout, connection loss, retryable server error) is reconciled by re-listing the target row once, and the file is **never POSTed a second time in the same run**:
  - listed → counted as uploaded;
  - not listed → reported as *unconfirmed* (an error). Large PDFs can still be processing server-side, so the next run's fresh listing decides whether to upload it;
  - the listing itself fails → reported as *unresolved* (an error), and no further uploads are attempted on that row.

  The Smartsheet SDK re-sends requests at two layers: its own 4001-4004 retry loop (`max_retry_time`) and a urllib3 `Retry` on its HTTP session adapter that re-sends a POST once after a read error. Both are disabled on the upload client (reads keep the SDK defaults). If the adapter retry cannot be disabled, the job refuses to start.
- **Temp files:** downloads go to uniquely named temporary files that are always removed.

### Exit Status

| Exit | Meaning |
|------|---------|
| `0` | No unresolved errors. Expected skips (invalid placeholders, unmatched or ambiguous WRs, existing filenames, non-PDF files) are not errors. |
| `1` | Configuration or routing check failed, a fatal error occurred, or at least one operational error (failed listing, failed download, signature rejection, rejected or unresolved upload). |
| `130` | Interrupted. |

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
├── tests/
│   ├── test_attachment_sync.py      # Matching, PDF rules, uploads (mocked)
│   └── test_config_and_main.py      # Routing checks, CLI modes, exit status
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
| `SOURCE_SHEET_ID` | int | 1553121697288068 | Project List (must equal the default) |
| `TARGET_SHEET_ID` | int | 573385022328708 | Project List - Blank Copy (must equal the default) |
| `SOURCE_MATCH_COLUMN_ID` | int | 8022925527175044 | Source `Work Request #` column (must equal the default) |
| `TARGET_MATCH_COLUMN_ID` | int | 7068369005875076 | Target `Work Request #` column (must equal the default) |
| `TEMP_DOWNLOAD_FOLDER` | string | /tmp/smartsheet-attachments | Temporary download location (created only with `--apply`) |

The routing IDs are pinned in `src/config.py` (`VERIFIED_ROUTING`). Any other value, the same sheet for source and target, or the retired target 4850043816202116 is rejected before the syncer starts.

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

Illustrative output of an `--apply` run (counts and IDs are examples):

```
2026-11-17 05:00:15 - INFO - Mode: APPLY (eligible PDFs will be uploaded to the target sheet)
2026-11-17 05:00:15 - INFO - ============================================================
2026-11-17 05:00:15 - INFO - 🚀 Starting Smartsheet PDF Attachment Sync (APPLY)
2026-11-17 05:00:15 - INFO - ============================================================
2026-11-17 05:00:16 - INFO - 📋 Loading source sheet: 1553121697288068
2026-11-17 05:00:17 - INFO - 📋 Loading target sheet: 573385022328708
2026-11-17 05:00:17 - INFO - ✅ Source preflight OK: sheet 1553121697288068, column 8022925527175044 'Work Request #', 150 rows
2026-11-17 05:00:17 - INFO - ✅ Target preflight OK: sheet 573385022328708, column 7068369005875076 'Work Request #', 148 rows
2026-11-17 05:00:17 - INFO - 🗺️ Indexing rows by 'Work Request #'...
2026-11-17 05:00:17 - INFO - 📊 Source: 150 rows, 141 distinct valid WRs, 3 blank, 4 invalid (e.g. ['WR-01', 'WR-02', 'CANCELED', 'Canceled'])
2026-11-17 05:00:17 - WARNING - ⚠️ Ambiguous WR 92873293: source rows [1234], target rows [5678, 5679]; skipped
2026-11-17 05:00:17 - INFO - 🔗 WRs matched: 135, unmatched: 5, ambiguous: 1
2026-11-17 05:00:20 - INFO - 🔗 Processing WR 92873294: source row 1235 → target row 5680
2026-11-17 05:00:21 - INFO - 📤 Uploading permit.pdf to target row 5680
2026-11-17 05:00:22 - INFO - ✅ Successfully copied: permit.pdf
2026-11-17 05:00:22 - INFO - ⏭️ Skipping existing filename on target row 5680: drawing.pdf
2026-11-17 05:00:40 - INFO - 📊 SYNC SUMMARY (APPLY)
2026-11-17 05:00:40 - INFO - WRs matched: 135
2026-11-17 05:00:40 - INFO - Non-PDF files excluded: 60
2026-11-17 05:00:40 - INFO - PDFs planned (new filenames): 42
2026-11-17 05:00:40 - INFO - PDFs content-verified (%PDF- signature): 42
2026-11-17 05:00:40 - INFO - Skipped (filename already on target row): 18
2026-11-17 05:00:40 - INFO - Uploads confirmed: 42
2026-11-17 05:00:40 - INFO - Uploads unresolved: 0
2026-11-17 05:00:40 - INFO - Errors: 0
2026-11-17 05:00:40 - INFO - ✅ Sync completed with no errors
```

The summary lists every counter: source/target rows, blank and invalid WRs, matched/unmatched/ambiguous WRs, FILE attachments seen, files excluded for an unsupported parent (sheet-level, proof), non-PDF exclusions, metadata conflicts, PDFs skipped for unsupported filename characters, planned and content-verified PDFs, signature rejections, existing and duplicate filenames, confirmed, unconfirmed and unresolved uploads, uploads stored under a different name, and errors. Unconfirmed, unresolved and name-mismatch uploads all count as errors. In a dry run, "planned" PDFs are metadata candidates only; "content-verified" stays 0 because nothing is downloaded.

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

#### "Routing: ..." or "Routing check failed, nothing was written"

**Explanation:** The effective sheet/column IDs are not the verified ones (for example the old target 4850043816202116), the source and target are the same sheet, a configured match column is missing, or it is no longer titled `Work Request #`. The job stops before any write. Fix the sheet or remove the stale environment override; retargeting requires a reviewed code change to `src/config.py` and the workflow.

#### WRs unmatched, invalid or ambiguous

**Explanation:** Normal skips, not errors. Unmatched WRs have no row in the target (rows are never created; individual WRs are logged at DEBUG). Invalid values such as `WR-01` or `CANCELED` are counted with a few examples. An ambiguous WR appears more than once in a sheet; the warning names the row IDs so the duplicate can be resolved in Smartsheet.

#### "attachment state is unknown" / "UNRESOLVED upload"

**Explanation:** An attachment listing failed or was incomplete, or an upload outcome could not be confirmed. Nothing further is written to that row in this run and the exit status is 1. Check the target row in Smartsheet before the next run; a later run skips any filename that is already there.

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

The `smartsheet` and `urllib3` loggers stay at WARNING even in debug mode: the SDK logs full API response bodies at DEBUG, and an attachment-details response contains a signed download URL. Do not lower those two loggers.

## Running the Tests

The test suite is offline: Smartsheet clients, downloads and timers are mocked, so no credentials or network are needed.

```bash
python -m unittest discover -s tests -t . -v
```

## Security Notes

- ⚠️ **Never commit your `.env` file** - It contains sensitive API keys
- ⚠️ **Use GitHub Secrets** for production credentials
- ⚠️ **Rotate API keys regularly** for security
- ⚠️ **Grant minimum required permissions** to the API key

## Dependencies

- `smartsheet-python-sdk==4.4.0` - Official Smartsheet Python SDK. **Pinned** on purpose: the upload safety relies on SDK internals (its retry loop, its session's urllib3 retry adapter, and its exception classes) that were verified against 4.4.0 only. Re-verify those, and the tests, before changing the version.
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
