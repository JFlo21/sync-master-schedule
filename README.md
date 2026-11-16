[![Smartsheet Attachment Sync](https://github.com/JFlo21/sync-master-schedule/actions/workflows/sync-attachments.yml/badge.svg)](https://github.com/JFlo21/sync-master-schedule/actions/workflows/sync-attachments.yml)

# Smartsheet Attachment Sync

Copies file attachments from **Project List** rows to the matching, already-existing rows of two target sheets, matched by `Work Request #`: **all files** to the **Master Schedule**, and **PDF files only** to **Project List - Blank Copy**. Runs daily at 5:00 AM UTC via GitHub Actions.

## Overview

This is an **attachment-only** sync. For each route it reads the source sheet, finds the target row with the same `Work Request #`, and attaches the source row's eligible files to that target row. It never creates, updates, moves, or deletes rows or cells, and never deletes or replaces attachments. The only Smartsheet write it performs is attaching a file to an existing, verified target row.

### Routes

The job runs two pinned routes, **in this order**, each with its own counters:

| # | Route | Source sheet / match column (title) | Target sheet / match column (title) | File rule |
|---|-------|-------------------------------------|-------------------------------------|-----------|
| 1 | `master-schedule` | Project List 1553121697288068 / 8022925527175044 (`Work Request #`) | Master Schedule 4850043816202116 / 1437881091182468 (`WR`) | `all_files`: every FILE attachment (PDFs, spreadsheets, images, ...) |
| 2 | `blank-copy` | Project List 1553121697288068 / 8022925527175044 (`Work Request #`) | Project List - Blank Copy 573385022328708 / 7068369005875076 (`Work Request #`) | `pdf_only`: PDF files only |

Both match columns hold the Work Request number; only their titles differ. Each column's exact title is pinned per route and checked before any write, so the titles are not interchangeable.

The Master Schedule runs first so the large first Blank Copy backfill cannot delay it. A failure in one route (including a routing or preflight failure) is logged and does not stop the other route; the exit status reports both.

### Sync Flow

```
Project List (1553121697288068)
├── 1. master-schedule → Master Schedule (4850043816202116), all files
└── 2. blank-copy      → Project List - Blank Copy (573385022328708), PDFs only (%PDF- signature checked)
Each route:
├── Match: Work Request number ("Work Request #" on Project List and Blank Copy, "WR" on Master Schedule)
├── Preflight: sheet IDs, column IDs and column titles verified before any write
├── Copy: eligible FILE attachments per the route's file rule
└── Skip: existing filenames, unmatched/invalid/ambiguous WRs, ineligible files
```

Routing is pinned in code (`src/config.py`, `VERIFIED_ROUTES`) and each target is bound to its file rule: a syncer for the Blank Copy with `all_files`, or for the Master Schedule with `pdf_only`, cannot be constructed.

## Features

- ✅ **Automatic Daily Sync** - Runs at 5:00 AM UTC via GitHub Actions with `--apply`
- 🧪 **Dry Run by Default** - `python main.py` plans only; uploads need `--apply`
- 🔒 **Pinned Routing** - Both routes (sheet IDs, column IDs, file rule) are pinned in code and checked against the live sheets before any write; routing environment variables are ignored
- 🔢 **Exact WR Matching** - `92873293`, `92873293.0`, `"92873293"` and `" 92873293.0 "` match; placeholders such as `WR-01` or `CANCELED` are skipped
- 📄 **Per-Route File Rule** - Master Schedule: every FILE attachment. Blank Copy: real PDF files only (MIME + `%PDF-` signature). Links and cloud shortcuts are never copied
- 🛡️ **Filename Duplicate Prevention** - Skips a file whose filename already exists on the target row
- 🔁 **Safe Retries** - Uploads are never blindly retried; an ambiguous upload is reconciled by re-listing the target row
- 🧹 **Auto Cleanup** - Removes temporary files after processing
- 📈 **Statistics and Truthful Exit Status** - Every count is reported; unresolved errors exit nonzero

## Prerequisites

- Python 3.11 or higher
- Smartsheet API key with read access to Project List and write access to both target sheets
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
```

Downloads go to a fixed folder under the system temp directory (`<system temp>/smartsheet-attachments`, i.e. `/tmp/smartsheet-attachments` on the Actions runner). It has no setting of its own; the system temp directory itself still follows the OS variables `TMPDIR`/`TEMP`/`TMP`.

There are no routing variables. Routing is pinned in code (`src/config.py`, `VERIFIED_ROUTES`); `SOURCE_SHEET_ID`, `TARGET_SHEET_ID`, `SOURCE_MATCH_COLUMN_ID` and `TARGET_MATCH_COLUMN_ID` are ignored if set, and the job logs one warning naming them. Retargeting is a reviewed code change, not a config override.

### 5. Run the Sync

```bash
python main.py --dry-run   # plan only, both routes: matching + candidate planning, no downloads, no uploads
python main.py --apply     # both routes: download, verify and upload eligible files
python main.py             # same as --dry-run (safe default)
```

A dry run still reads the sheets, the source attachment listings, and the attachment listings of target rows that have candidates (to compare filenames), but it never downloads a file and never writes to Smartsheet.

Inside GitHub Actions (`GITHUB_ACTIONS=true`) a mode flag is required: `python main.py` with no flag exits 1 before any API call, so a workflow that forgets the flag fails loudly instead of running a silent dry run. Local runs keep the dry-run default. "Planned" files in a dry run are metadata candidates; only `--apply` downloads them (and, on the Blank Copy route, checks the PDF signature).

## GitHub Actions Setup

### Required Secrets

Navigate to your repository's **Settings → Secrets and variables → Actions**:

| Secret Name | Required | Description |
|-------------|----------|-------------|
| `SMARTSHEET_API_KEY` | ✅ Yes | Your Smartsheet API key (the only secret) |

Routing IDs are **not** secrets and are not passed by the workflow: both routes are pinned in `src/config.py`. Any old `SOURCE_SHEET_ID`, `TARGET_SHEET_ID`, `SOURCE_MATCH_COLUMN_ID` or `TARGET_MATCH_COLUMN_ID` repository secrets are unused (and ignored with a warning if they ever reach the job). They can be deleted.

### Workflow Schedule

The sync runs:
- **Daily at 5:00 AM UTC** via cron schedule, with `--apply`
- **Manual trigger** via workflow_dispatch, with a **mode** choice: `dry-run` (default) or `apply`

To manually trigger:
1. Go to **Actions** tab in your repository
2. Select **Smartsheet Attachment Sync** workflow
3. Click **Run workflow**, choose the mode (`dry-run` unless you intend to upload), and run

Only one run at a time can work on the targets: scheduled and manual runs share a concurrency group (`smartsheet-attachment-sync`, `cancel-in-progress: false`). A new run waits; an active run is never cancelled mid-upload. A local `python main.py --apply` is **not** covered by this concurrency group; do not run it while a workflow run is active.

## How It Works

### Sync Logic

The steps below run once per route (Master Schedule first, then Blank Copy). Every log line of a route is prefixed with the route name, e.g. `[master-schedule]`.

1. **Load and verify sheets** - Loads the source and the route's target sheet and checks, before any write, that each sheet ID is the pinned one, that the pinned match column exists and carries exactly the title pinned for that sheet in that route (`Work Request #`, or `WR` on the Master Schedule), and that every row was loaded: the response must carry an integer total row count equal to the rows received. If the first response holds fewer rows than that total, the remaining pages are fetched with explicit paging and combined; the combined rows must match the total exactly with no repeated row IDs. Any mismatch stops that route (nothing written to it), the other route still runs, and the exit status is 1.
2. **Index Work Request #** - Builds an exact key for each row (see matching rules).
3. **Match** - For each valid source WR: skip it if it appears more than once in either sheet (ambiguous, row IDs reported), skip it if the target has no such row (rows are never created), otherwise pair the source row with the target row ID returned by the API.
4. **Plan files** - Lists each matched source row's FILE attachments and keeps the candidates allowed by the route's file rule.
5. **Copy (apply only)** - Lists the target row's attachments just in time, then for each new filename downloads the file with a fresh URL and uploads the file bytes with the original filename. Blank Copy: the `%PDF-` signature is checked first and the upload MIME type is `application/pdf`. Master Schedule: no content check; the upload MIME type is the source attachment's MIME type (from the attachment details, else from the listing, else `application/octet-stream`; a generic `application/octet-stream` / `binary/octet-stream` value never beats a specific one).
6. **Report** - Logs every counter for the route, then one result line per route and an overall line, and sets the exit status.

> **Behavior change for the Master Schedule:** the previous production job used the *last* matching row when a WR appeared more than once. A WR repeated in either sheet is now skipped and reported with its row IDs on both routes.

### Work Request # Matching

- Integer-like values match exactly: `92873293`, `92873293.0`, `"92873293"` and `" 92873293.0 "` are the same WR. Text is parsed exactly (ASCII digits, optional `.0`), never through a float conversion, and characters are never stripped to manufacture a match.
- Blanks, booleans, fractions (`1.5`), non-finite numbers, and placeholders (`WR-01`, `WR-02`, `CANCELED`, `Canceled`, ...) are skipped and counted, never matched to each other.
- Matching never uses the displayed Row ID, Scope #, row position, filenames, or helper formulas.

### File Rules

Shared by both routes:

- Only FILE attachments of the matched source row are considered, including files on that row's discussions (parent type `ROW` or `COMMENT`). Links, cloud-storage shortcuts, comment text, sheet-level attachments and proofs are never copied; files with another parent are skipped with a warning.
- When the listing has no MIME type, the attachment details are read to resolve it; if that read fails, an error is counted and the file is skipped.
- A file whose filename contains a double quote or a control character is skipped with a warning: the upload would store it under a percent-encoded name, so the filename duplicate check could never recognise it and every run would upload it again. If the server ever reports storing a file under a different name, the run reports an error.
- Downloaded files are written to uniquely named temporary files whose names never come from the attachment name.
- The same rules apply whether source metadata came from the pre-fetched cache or a direct listing.

**Master Schedule (`all_files`):** every FILE attachment that passes the shared rules is copied: PDFs, spreadsheets, images, anything. There is no PDF classification, no metadata-conflict skip and no signature check. The upload MIME type is the source's MIME type, chosen in this order: the type from the attachment details (the same `get_attachment` call that provides the download URL, as in the previous production job), then the listing type, then `application/octet-stream`. Malformed and generic (`application/octet-stream`, `binary/octet-stream`) values are skipped until that final fallback; the chosen value is sent as `type/subtype`, lower-cased, without parameters.

The file rule itself fails closed: a route whose rule is not `all_files` or `pdf_only` is rejected before any client is built, so a typo can never turn into "copy every file".

**Blank Copy (`pdf_only`):**

- MIME type `application/pdf` is a PDF candidate. When the MIME type is missing or generic (`application/octet-stream`), a case-insensitive `.pdf` extension is accepted as a fallback. A `.pdf` name with a different MIME type is a conflict and is skipped with a warning.
- "PDF" means an actual PDF file, not a filename containing "Adobe" or a file produced by a particular application. Nothing is converted to PDF.
- Before upload, the downloaded file must contain the `%PDF-` signature within its first 1024 bytes (where PDF readers look for it). HTML error pages and renamed non-PDFs are rejected and counted as errors.

### Duplicate Policy (filename-based, both routes)

A file is skipped when a FILE attachment with the same filename (exact, case-sensitive) already exists on the target row. This is **filename-based**, not content or version synchronization: a changed file re-uploaded under the same name in the source is not copied again, and existing target attachments are never replaced or deleted. A filename repeated on the same source row is copied once and the collision is reported. Once an upload has been sent for a filename, a same-named file is not sent to that row again in the same run, even if the first upload was rejected or could not be confirmed. After a confirmed upload the filename is recorded immediately, so a repeated run adds nothing.

### Failure Handling

- **Listings:** attachment listings are complete (`include_all`) and retried with bounded backoff. A listing that fails or comes back incomplete makes the row's state *unknown*: nothing is written to that row and an error is counted. It is never treated as empty.
- **Downloads:** bounded attempts, each with a fresh download URL. A body shorter than the declared `Content-Length` counts as a failed attempt. Download URLs are signed and are never logged.
- **Uploads:** at most one upload starts every 2.5 seconds (Smartsheet allows 30 attachment uploads per minute). A rate-limit rejection (nothing stored) waits and retries. A definite rejection (e.g. permissions) is not retried. An ambiguous outcome (timeout, connection loss, retryable server error) is reconciled by re-listing the target row once, and the file is **never POSTed a second time in the same run**:
  - listed → counted as uploaded;
  - not listed → reported as *unconfirmed* (an error). Large files can still be processing server-side, so the next run's fresh listing decides whether to upload it;
  - the listing itself fails → reported as *unresolved* (an error), and no further uploads are attempted on that row.

  The Smartsheet SDK re-sends requests at two layers: its own 4001-4004 retry loop (`max_retry_time`) and a urllib3 `Retry` on its HTTP session adapter that re-sends a POST once after a read error. Both are disabled on the upload client (reads keep the SDK defaults). If the adapter retry cannot be disabled, the job refuses to start.
- **Temp files:** downloads go to uniquely named temporary files that are always removed.

### Exit Status

| Exit | Meaning |
|------|---------|
| `0` | Both routes finished with no unresolved errors. Expected skips (invalid placeholders, unmatched or ambiguous WRs, existing filenames, non-PDF files on the Blank Copy route) are not errors. |
| `1` | Configuration check failed, or in either route a routing/preflight check failed, a fatal error occurred, or at least one operational error was counted (failed listing or metadata read, failed download, signature rejection, rejected or unresolved upload). The other route still runs. |
| `130` | Interrupted (stops both routes). |

## Project Structure

```
sync-master-schedule/
├── .github/
│   └── workflows/
│       └── sync-attachments.yml     # GitHub Actions workflow
├── src/
│   ├── __init__.py                  # Package initialization
│   ├── config.py                    # Pinned routes (VERIFIED_ROUTES), validate_route
│   └── attachment_sync.py           # Core sync logic (one syncer per route)
├── tests/
│   ├── test_attachment_sync.py      # Matching, file rules, uploads (mocked)
│   └── test_config_and_main.py      # Route table, CLI modes, per-route exit status
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

The download temp folder is fixed under the system temp directory (`<system temp>/smartsheet-attachments`, created only with `--apply`); `TEMP_DOWNLOAD_FOLDER` is no longer read.

Routing is pinned in `src/config.py` (`VERIFIED_ROUTES`, see [Routes](#routes)) and is not read from the environment. `SOURCE_SHEET_ID`, `TARGET_SHEET_ID`, `SOURCE_MATCH_COLUMN_ID` and `TARGET_MATCH_COLUMN_ID` are ignored if set (one warning is logged). The syncer refuses any route that is not exactly one of the verified routes: an unknown target, the same sheet for source and target, the wrong file rule for a target, a mismatched column, or a non-integer ID.

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
2026-11-17 05:00:15 - INFO - Mode: APPLY (eligible files will be uploaded to the target sheets)
2026-11-17 05:00:15 - INFO - [master-schedule] ============================================================
2026-11-17 05:00:15 - INFO - [master-schedule] 🚀 Starting Smartsheet Attachment Sync, route 'master-schedule' (all file attachments): sheet 1553121697288068 → sheet 4850043816202116 (APPLY)
2026-11-17 05:00:17 - INFO - [master-schedule] ✅ Target preflight OK: sheet 4850043816202116, column 1437881091182468 'WR', 148 rows
2026-11-17 05:00:17 - WARNING - [master-schedule] ⚠️ Ambiguous WR 92873293: source rows [1234], target rows [5678, 5679]; skipped
2026-11-17 05:00:21 - INFO - [master-schedule] 📤 Uploading costs.xlsx to target row 5680
2026-11-17 05:00:22 - INFO - [master-schedule] ✅ Successfully copied: costs.xlsx
2026-11-17 05:00:40 - INFO - [master-schedule] 📊 SYNC SUMMARY, route 'master-schedule' (APPLY)
2026-11-17 05:00:40 - INFO - [master-schedule] Files planned (new filenames): 12
2026-11-17 05:00:40 - INFO - [master-schedule] Uploads confirmed: 12
2026-11-17 05:00:40 - INFO - [master-schedule] Errors: 0
2026-11-17 05:00:41 - INFO - [blank-copy] 🚀 Starting Smartsheet Attachment Sync, route 'blank-copy' (PDF files only): sheet 1553121697288068 → sheet 573385022328708 (APPLY)
2026-11-17 05:00:43 - INFO - [blank-copy] 📤 Uploading permit.pdf to target row 7680
2026-11-17 05:00:44 - INFO - [blank-copy] ✅ Successfully copied: permit.pdf
2026-11-17 05:00:44 - INFO - [blank-copy] ⏭️ Skipping existing filename on target row 7680: drawing.pdf
2026-11-17 05:01:10 - INFO - [blank-copy] 📊 SYNC SUMMARY, route 'blank-copy' (APPLY)
2026-11-17 05:01:10 - INFO - [blank-copy] Non-PDF files excluded: 60
2026-11-17 05:01:10 - INFO - [blank-copy] PDFs planned (new filenames): 42
2026-11-17 05:01:10 - INFO - [blank-copy] PDFs content-verified (%PDF- signature): 42
2026-11-17 05:01:10 - INFO - [blank-copy] Uploads confirmed: 42
2026-11-17 05:01:10 - INFO - [blank-copy] Errors: 0
2026-11-17 05:01:10 - INFO - ============================================================
2026-11-17 05:01:10 - INFO - ✅ Route master-schedule: completed with no errors
2026-11-17 05:01:10 - INFO - ✅ Route blank-copy: completed with no errors
2026-11-17 05:01:10 - INFO - ✅ All 2 routes completed with no errors
```

Each route's summary lists every counter: source/target rows, blank and invalid WRs, matched/unmatched/ambiguous WRs, WRs duplicated in the target only (no source row; warned with their row IDs, not an error), FILE attachments seen, files excluded for an unsupported parent (sheet-level, proof), files skipped for unsupported filename characters, planned files, existing and duplicate filenames, confirmed, unconfirmed and unresolved uploads, uploads stored under a different name, and errors. The Blank Copy summary also lists non-PDF exclusions, metadata conflicts, content-verified PDFs and signature rejections. Unconfirmed, unresolved and name-mismatch uploads all count as errors. In a dry run, "planned" files are metadata candidates only; nothing is downloaded, so "content-verified" stays 0.

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

#### "Routing check failed, nothing was written for this route"

**Explanation:** For the named route, a loaded sheet is not the pinned one, its match column is missing, or the column's title no longer matches the title pinned for that sheet in that route (`Work Request #` on Project List and Blank Copy, `WR` on the Master Schedule; exact, case-sensitive). The error names the route, the column ID, the expected title and the actual title (or, after a bad code change, the route is not one of `VERIFIED_ROUTES`). Nothing is written to that route's target, the other route still runs, and the exit status is 1. Fix the sheet (for example rename the column back); a deliberate rename, like retargeting, requires a reviewed code change to the route in `src/config.py`.

#### "Ignoring routing environment variables ..."

**Explanation:** A legacy `SOURCE_SHEET_ID` / `TARGET_SHEET_ID` / `SOURCE_MATCH_COLUMN_ID` / `TARGET_MATCH_COLUMN_ID` value is set. It has no effect; remove it from the environment or `.env`.

#### WRs unmatched, invalid or ambiguous

**Explanation:** Normal skips, not errors. Unmatched WRs have no row in the target (rows are never created; individual WRs are logged at DEBUG). Invalid values such as `WR-01` or `CANCELED` are counted with a few examples. An ambiguous WR appears more than once in a sheet; the warning names the row IDs so the duplicate can be resolved in Smartsheet.

#### "attachment state is unknown" / "UNRESOLVED upload"

**Explanation:** An attachment listing failed or was incomplete, or an upload outcome could not be confirmed. Nothing further is written to that row in this run and the exit status is 1. Check the target row in Smartsheet before the next run; a later run skips any filename that is already there.

#### Permission Denied Errors

**Solution:** Ensure your Smartsheet API key has:
- Read access to the source sheet (Project List)
- Write access to both target sheets (Master Schedule and Project List - Blank Copy)
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
