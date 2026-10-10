# Academic Result PDF to CSV Converter

Version 0.3.0 — starter project for extracting result rows from text-based PDFs.

## What it does

- Uses `pdfplumber` to find tables in PDFs with extractable text.
- Checks identifiers, scores, sessions, semesters, course codes, and attempt numbers.
- Writes `results.csv`, `issues.csv`, `trace.csv`, and `extraction-report.json`.
- Holds unclear rows for review instead of guessing.

## Important limitation

This version does **not** perform OCR. Scanned/image-only pages are reported for review. It also does not connect to a database or import records into Django.

PDF layouts vary, so test it against a few synthetic or redacted documents before relying on it. Always review accepted rows against the source PDF.

## Setup in VS Code (Windows)

Open this folder in VS Code. In the VS Code terminal, run:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell blocks activation, use Command Prompt and run:

```bat
.venv\Scripts\activate.bat
```

## Run it

Example:

```powershell
python main.py "sample_results.pdf" --output-dir output --academic-session 2026/2027 --semester harmattan --identifier-type matriculation --attempt-number 1
```

Only supply context values after checking the PDF or receiving reviewer approval. Do not supply attempt number `1` merely to make rows pass validation.

For a PDF whose tables do not have drawn grid lines, try:

```powershell
python main.py "sample_results.pdf" --table-strategy text --output-dir output
```

## Output files

- `results.csv`: accepted rows only; not necessarily the complete academic history.
- `issues.csv`: rows or pages that need review, with diagnostic codes.
- `trace.csv`: source page/table/row and source credit-unit/grade values.
- `extraction-report.json`: counts, input SHA-256, settings, warnings, and status.

## Exit codes

- `0`: at least one row accepted and row counts reconcile.
- `1`: hard failure or row-count mismatch.
- `2`: conversion ran but no rows were accepted.

## Tests

Run:

```powershell
python -m unittest discover -s tests -v
```

The included tests cover helper functions only. They are not a substitute for end-to-end tests on representative PDF layouts.

## Privacy

Do not commit real student result PDFs, identifiers, or generated result files to GitHub. Use synthetic or redacted fixtures.
