"""
Academic Result PDF -> CSV Converter
Version: 0.3.0

Supports text-based PDFs with tables detectable by pdfplumber.
OCR is not implemented: scanned pages are reported for manual/OCR review.

Outputs:
  results.csv
  issues.csv
  trace.csv
  extraction-report.json
"""

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pdfplumber

VERSION = "0.3.0"
LOGGER = logging.getLogger("results_converter")

RESULT_HEADERS = [
    "identifier_type",
    "identifier_value",
    "course_code",
    "academic_session",
    "semester",
    "attempt_number",
    "score",
]
ISSUE_HEADERS = [
    "source_file",
    "page",
    "table",
    "source_row",
    "source_cell",
    "raw_value",
    "diagnostic_code",
    "explanation",
]
TRACE_HEADERS = [
    "output_row_number",
    "source_file",
    "page",
    "table",
    "source_row",
    "source_credit_units",
    "source_grade_label",
    "context_origin",
]
OUTPUT_NAMES = [
    "results.csv",
    "issues.csv",
    "trace.csv",
    "extraction-report.json",
]

SEMESTER_MAP_DEFAULT = {
    "harmattan": "harmattan",
    "rain": "rain",
}
VALID_SEMESTERS = {"harmattan", "rain"}
HEADER_SEARCH_ROWS = 5

FIELD_ALIASES = {
    "identifier": {
        "matric", "matricno", "matricnumber", "matriculation",
        "matriculationno", "matriculationnumber", "regno", "regnumber",
        "registrationno", "registrationnumber", "utme", "utmeno",
        "utmenumber", "jamb", "jambno", "jambnumber", "identifier",
        "studentid", "studentno",
    },
    "score": {"score", "totalscore", "total", "mark", "marks", "finalscore"},
    "course": {"course", "coursecode"},
    "session": {"session", "academicsession"},
    "semester": {"semester"},
    "attempt": {"attempt", "attemptno", "attemptnumber"},
    "units": {"unit", "units", "creditunit", "creditunits", "cu"},
    "grade": {"grade", "gradelabel"},
}

SESSION_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})\s*[/\-]\s*((?:19|20)\d{2})(?!\d)"
)
COURSE_RE = re.compile(
    r"^\s*(?:course(?:\s*code)?\s*[:\-]?\s*)?"
    r"([A-Za-z]{2,4}\s?\d{3}[A-Za-z]?)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
COURSE_STOP_PREFIXES = {"PAGE", "TABLE", "FIG", "ROOM", "TEL", "FAX"}
SCORE_RE = re.compile(r"^\d+(?:\.\d{1,2})?$")
SCI_NOTATION_RE = re.compile(r"^\d+(?:\.\d+)?[eE][+-]?\d+$")
FLOAT_ZERO_RE = re.compile(r"^\d+\.0+$")
ATTEMPT_RE = re.compile(r"^\d+$")
MISSING_SCORE_MARKERS = {
    "", "abs", "absent", "-", "--", "n/a", "na", "pending", "nil",
    "inc", "incomplete", "withheld",
}


# ---------------------------------------------------------------------
# Basic helper functions
# ---------------------------------------------------------------------

def clean(value):
    """Convert a cell to trimmed, single-spaced text."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def normalize_header(value):
    """Make header labels comparable: 'Matric No.' -> 'matricno'."""
    return re.sub(r"[^a-z0-9]", "", clean(value).casefold())


def compact_course(value):
    """Remove whitespace inside a course code without changing its case."""
    return re.sub(r"\s+", "", value or "")


def normalize_session(value):
    """Return YYYY/YYYY only when the years are consecutive."""
    match = SESSION_RE.search(clean(value))
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    if end != start + 1:
        return None
    return f"{start}/{end}"


def normalize_semester(value, semester_map):
    """Map an approved source label to 'harmattan' or 'rain'."""
    return semester_map.get(clean(value).casefold())


def parse_score(raw):
    """Return (score, error); valid scores are text from 0.00 to 100.00."""
    value = clean(raw)
    if value.casefold() in MISSING_SCORE_MARKERS:
        shown = value if value else "(blank)"
        return None, (
            "MISSING_SCORE",
            f"Score is {shown}; it has not been converted to zero.",
        )
    if value.startswith("-") and value[1:].replace(".", "", 1).isdigit():
        return None, ("INVALID_SCORE", "Score cannot be negative.")
    if not SCORE_RE.fullmatch(value):
        return None, (
            "UNREADABLE_SCORE",
            "Score is not a plain number with at most two decimal places.",
        )

    try:
        number = Decimal(value)
    except InvalidOperation:
        return None, ("UNREADABLE_SCORE", "Score could not be parsed.")

    if number < 0 or number > 100:
        return None, ("INVALID_SCORE", "Score must be between 0 and 100.")

    return f"{number:.2f}", None


def check_identifier(raw):
    """Avoid guessing or silently repairing extracted identifiers."""
    if not raw:
        return ("MISSING_IDENTIFIER", "Identifier cell is blank.")
    if re.search(r"\s", raw):
        return (
            "AMBIGUOUS_IDENTIFIER",
            "Identifier contains internal whitespace; check the PDF.",
        )
    if SCI_NOTATION_RE.fullmatch(raw) or FLOAT_ZERO_RE.fullmatch(raw):
        return (
            "AMBIGUOUS_IDENTIFIER",
            "Identifier may have been changed by numeric conversion.",
        )
    return None


def parse_attempt(raw):
    value = clean(raw)
    if not ATTEMPT_RE.fullmatch(value):
        return None, ("INVALID_ATTEMPT", "Attempt must be a whole number.")
    number = int(value)
    if not 1 <= number <= 32767:
        return None, (
            "INVALID_ATTEMPT",
            "Attempt number must be between 1 and 32767.",
        )
    return number, None


def detect_header(row):
    """Return (column mapping, identifier hint, error) for a header row."""
    mapping = {}
    identifier_hint = None

    for index, cell in enumerate(row):
        label = normalize_header(cell)
        if not label:
            continue

        for field, aliases in FIELD_ALIASES.items():
            if label not in aliases:
                continue
            if field in mapping:
                return None, None, f"More than one '{field}' header column."
            mapping[field] = index

            if field == "identifier":
                if "matric" in label or label.startswith(("reg", "registration")):
                    identifier_hint = "matriculation"
                elif "utme" in label or "jamb" in label:
                    identifier_hint = "utme"
            break

    if "identifier" in mapping and "score" in mapping:
        return mapping, identifier_hint, None
    return None, None, None


def find_heading_context(lines, semester_map):
    """Read context values from text outside the detected table."""
    context = {"academic_session": set(), "semester": set(), "course_code": set()}
    invalid_session = []

    labels = sorted(semester_map, key=len, reverse=True)
    for line in lines:
        text = clean(line)
        if not text:
            continue

        for match in SESSION_RE.finditer(text):
            normalized = normalize_session(match.group(0))
            if normalized:
                context["academic_session"].add(normalized)
            else:
                invalid_session.append(match.group(0))

        remaining = text.casefold()
        for label in labels:
            pattern = r"(?<!\w)" + re.escape(label) + r"(?!\w)"
            if re.search(pattern, remaining):
                context["semester"].add(semester_map[label])
                remaining = re.sub(pattern, " ", remaining)

        match = COURSE_RE.match(text)
        if match:
            course = compact_course(match.group(1))
            prefix_match = re.match(r"[A-Za-z]+", course)
            prefix = prefix_match.group(0).upper() if prefix_match else ""
            if prefix and prefix not in COURSE_STOP_PREFIXES:
                context["course_code"].add(course)

    return context, invalid_session


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_csv(path, headers, rows):
    """Write a correctly quoted UTF-8 CSV."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=headers, extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def load_semester_map(path):
    mapping = dict(SEMESTER_MAP_DEFAULT)
    if not path:
        return mapping

    extra = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(extra, dict):
        raise ValueError("Semester map JSON must be an object.")

    for label, semester in extra.items():
        normalized_label = clean(label).casefold()
        if not normalized_label:
            raise ValueError("Semester map labels cannot be blank.")
        if semester not in VALID_SEMESTERS:
            raise ValueError(
                f"Semester mapping '{label}' must map to harmattan or rain."
            )
        mapping[normalized_label] = semester
    return mapping


# ---------------------------------------------------------------------
# Converter
# ---------------------------------------------------------------------

class Converter:
    def __init__(self, source_name, supplied, semester_map, strategy):
        self.source_name = source_name
        self.supplied = supplied
        self.semester_map = semester_map
        self.table_settings = {
            "vertical_strategy": strategy,
            "horizontal_strategy": strategy,
        }

        self.candidates = []
        self.issues = []
        self.unresolved_rows = set()
        self.source_rows = 0
        self.blank_rows = 0
        self.header_rows = 0
        self.heading_rows = 0
        self.pages_without_text = []
        self.pages_without_tables = []
        self.warnings = []
        self.context_carried = []

    def add_issue(self, page, table, row, cell, raw, code, explanation,
                  counts_as_row=True):
        self.issues.append({
            "source_file": self.source_name,
            "page": page,
            "table": table if table is not None else "",
            "source_row": row if row is not None else "",
            "source_cell": cell,
            "raw_value": raw,
            "diagnostic_code": code,
            "explanation": explanation,
            "_sort": (page, table or 0, row or 0, len(self.issues)),
        })
        if counts_as_row and row is not None and table is not None:
            self.unresolved_rows.add((page, table, row))

    @staticmethod
    def outside_table_text(page, tables):
        boxes = [table.bbox for table in tables]

        def keep(obj):
            if obj.get("object_type") != "char":
                return True
            x = (obj["x0"] + obj["x1"]) / 2
            y = (obj["top"] + obj["bottom"]) / 2
            return not any(
                box[0] <= x <= box[2] and box[1] <= y <= box[3]
                for box in boxes
            )

        return page.filter(keep).extract_text() or ""

    def resolve_context(self, key, heading_value, column_value, page_no,
                        table_no, row_no, issues):
        supplied_value = self.supplied.get(key)
        values = []
        if supplied_value is not None:
            values.append((supplied_value, "supplied"))
        if heading_value is not None:
            values.append((heading_value, f"heading p{page_no}"))
        if column_value is not None:
            values.append((column_value, "column"))

        if not values:
            issues.append((
                key, "", "MISSING_CONTEXT",
                f"{key} was not found or supplied.",
            ))
            return None, None

        distinct = {value for value, _origin in values}
        if len(distinct) > 1:
            detail = "; ".join(f"{value} ({origin})" for value, origin in values)
            issues.append((
                key, "", "CONFLICTING_CONTEXT",
                f"{key} differs between sources: {detail}.",
            ))
            return None, None

        return values[0][0], "+".join(origin for _value, origin in values)

    def process_page(self, page, page_no):
        tables = page.find_tables(self.table_settings)
        page_text = page.extract_text() or ""

        if not page_text.strip() and not tables:
            self.pages_without_text.append(page_no)
            self.add_issue(
                page_no, None, None, "page", "", "UNREADABLE_PAGE",
                "No extractable text or table detected. This may be a scanned "
                "page; OCR is not implemented.",
                counts_as_row=False,
            )
            return

        if not tables:
            self.pages_without_tables.append(page_no)
            self.add_issue(
                page_no, None, None, "page", "", "UNSUPPORTED_LAYOUT",
                "Text was found, but no table was detected with the selected "
                "table strategy.",
                counts_as_row=False,
            )
            return

        # Avoid carrying a previous page's context blindly. Context is read
        # from this page, and missing context must be supplied explicitly.
        page_lines = self.outside_table_text(page, tables).splitlines()
        heading_context, invalid_sessions = find_heading_context(
            page_lines, self.semester_map
        )

        if invalid_sessions:
            self.add_issue(
                page_no, None, None, "heading", ", ".join(invalid_sessions),
                "INVALID_CONTEXT",
                "A session in the page heading does not contain consecutive years.",
                counts_as_row=False,
            )

        resolved_heading = {}
        for key in ("academic_session", "semester", "course_code"):
            found = heading_context[key]
            if len(found) > 1:
                resolved_heading[key] = None
                self.add_issue(
                    page_no, None, None, "heading", ", ".join(sorted(found)),
                    "AMBIGUOUS_CONTEXT",
                    f"Several {key} values appear in page headings.",
                    counts_as_row=False,
                )
            elif found:
                resolved_heading[key] = next(iter(found))
            else:
                resolved_heading[key] = None

        for table_no, table in enumerate(tables, start=1):
            self.process_table(
                table, page_no, table_no, resolved_heading
            )

    def process_table(self, table, page_no, table_no, heading_context):
        rows = [[clean(cell) for cell in row] for row in (table.extract() or [])]
        if not rows:
            self.add_issue(
                page_no, table_no, None, "table", "", "UNSUPPORTED_LAYOUT",
                "A table was detected but no rows could be extracted.",
                counts_as_row=False,
            )
            return

        header_index = None
        mapping = None
        identifier_hint = None
        header_error = None

        for index, row in enumerate(rows[:HEADER_SEARCH_ROWS]):
            possible_mapping, hint, error = detect_header(row)
            if error:
                header_error = error
                break
            if possible_mapping:
                header_index = index
                mapping = possible_mapping
                identifier_hint = hint
                break

        if header_index is None:
            self.add_issue(
                page_no, table_no, None, "table",
                " || ".join(" | ".join(row) for row in rows),
                "UNSUPPORTED_LAYOUT",
                header_error or (
                    "No identifier-and-score header found in the first "
                    f"{HEADER_SEARCH_ROWS} rows. This version does not "
                    "automatically reuse a previous table's column layout."
                ),
                counts_as_row=False,
            )
            self.warnings.append(
                f"Page {page_no}, table {table_no} was not extracted because "
                "its column header was not recognised."
            )
            return

        self.header_rows += 1
        self.heading_rows += sum(1 for row in rows[:header_index] if any(row))
        ncols = len(rows[header_index])

        for row_index in range(header_index + 1, len(rows)):
            cells = rows[row_index]
            source_row_no = row_index + 1
            if not any(cells):
                self.blank_rows += 1
                continue

            repeated_mapping, _hint, _error = detect_header(cells)
            if repeated_mapping:
                self.header_rows += 1
                continue

            self.handle_row(
                page_no, table_no, source_row_no, cells, mapping,
                identifier_hint, heading_context, ncols
            )

    def handle_row(self, page_no, table_no, row_no, cells, mapping,
                   identifier_hint, heading_context, expected_columns):
        self.source_rows += 1
        raw_row = " | ".join(cells)
        problems = []

        if len(cells) < expected_columns or max(mapping.values()) >= len(cells):
            problems.append((
                "row", raw_row, "UNSUPPORTED_LAYOUT",
                "This row has fewer cells than the detected table header.",
            ))
            self.record_row_issues(page_no, table_no, row_no, problems)
            return

        def get(field):
            index = mapping.get(field)
            return cells[index] if index is not None and index < len(cells) else ""

        identifier = get("identifier")
        score_raw = get("score")

        if not identifier and not score_raw:
            problems.append((
                "row", raw_row, "SPLIT_ROW",
                "Identifier and score are both blank. Possible split row; "
                "not merged automatically.",
            ))
            self.record_row_issues(page_no, table_no, row_no, problems)
            return

        identifier_problem = check_identifier(identifier)
        if identifier_problem:
            problems.append(("identifier", identifier, *identifier_problem))

        score, score_problem = parse_score(score_raw)
        if score_problem:
            problems.append(("score", score_raw, *score_problem))

        # Context may come from an explicit CLI option, a page heading,
        # or a row-level column. Conflicts are never resolved by guessing.
        column_session = None
        raw = get("session")
        if raw:
            column_session = normalize_session(raw)
            if column_session is None:
                problems.append((
                    "session", raw, "INVALID_CONTEXT",
                    "Session must be consecutive years, such as 2026/2027.",
                ))

        column_semester = None
        raw = get("semester")
        if raw:
            column_semester = normalize_semester(raw, self.semester_map)
            if column_semester is None:
                problems.append((
                    "semester", raw, "INVALID_CONTEXT",
                    "Semester label has no reviewed mapping.",
                ))

        column_course = compact_course(get("course")) or None
        attempt = None
        raw = get("attempt")
        if raw:
            attempt, attempt_problem = parse_attempt(raw)
            if attempt_problem:
                problems.append(("attempt", raw, *attempt_problem))

        identifier_type = self.supplied.get("identifier_type") or identifier_hint
        if not identifier_type:
            problems.append((
                "identifier_type", "", "MISSING_CONTEXT",
                "Identifier type is unknown. Supply --identifier-type "
                "matriculation or utme after reviewing the source.",
            ))

        resolved = {}
        origins = {}
        for key, heading_key, column_value in (
            ("academic_session", "academic_session", column_session),
            ("semester", "semester", column_semester),
            ("course_code", "course_code", column_course),
        ):
            value, origin = self.resolve_context(
                key, heading_context.get(heading_key), column_value,
                page_no, table_no, row_no, problems
            )
            resolved[key] = value
            origins[key] = origin

        if identifier_type:
            resolved["identifier_type"] = identifier_type
            origins["identifier_type"] = (
                "supplied" if self.supplied.get("identifier_type")
                else "header"
            )

        supplied_attempt = self.supplied.get("attempt_number")
        if attempt is not None and supplied_attempt is not None and attempt != supplied_attempt:
            problems.append((
                "attempt_number", str(attempt), "CONFLICTING_CONTEXT",
                f"Table attempt {attempt} conflicts with supplied attempt "
                f"{supplied_attempt}.",
            ))
        elif attempt is not None:
            resolved["attempt_number"] = str(attempt)
            origins["attempt_number"] = "column"
        elif supplied_attempt is not None:
            resolved["attempt_number"] = str(supplied_attempt)
            origins["attempt_number"] = "supplied"
        else:
            problems.append((
                "attempt_number", "", "MISSING_CONTEXT",
                "Attempt number is absent. Supply --attempt-number only "
                "when the source or reviewer establishes it.",
            ))

        if problems:
            self.record_row_issues(page_no, table_no, row_no, problems)
            return

        self.candidates.append({
            "row": {
                "identifier_type": resolved["identifier_type"],
                "identifier_value": identifier,
                "course_code": resolved["course_code"],
                "academic_session": resolved["academic_session"],
                "semester": resolved["semester"],
                "attempt_number": resolved["attempt_number"],
                "score": score,
            },
            "loc": (page_no, table_no, row_no),
            "units": get("units"),
            "grade": get("grade"),
            "origin": ";".join(
                f"{key}={origins[key]}"
                for key in (
                    "identifier_type", "academic_session", "semester",
                    "course_code", "attempt_number"
                )
            ),
        })

    def record_row_issues(self, page, table, row, problems):
        if not problems:
            problems = [(
                "row", "", "UNRESOLVED_ROW",
                "The row could not be accepted.",
            )]
        for cell, raw, code, explanation in problems:
            self.add_issue(
                page, table, row, cell, raw, code, explanation
            )

    def finish(self):
        """Reject every copy of a duplicate result key."""
        counts = Counter(
            tuple(candidate["row"][key] for key in RESULT_HEADERS[:6])
            for candidate in self.candidates
        )
        accepted = []
        for candidate in self.candidates:
            key = tuple(candidate["row"][field] for field in RESULT_HEADERS[:6])
            if counts[key] > 1:
                page, table, row = candidate["loc"]
                self.add_issue(
                    page, table, row, "row",
                    candidate["row"]["identifier_value"],
                    "DUPLICATE_ROW",
                    f"This result key appears {counts[key]} times. All copies "
                    "are held for review; no attempt was invented.",
                )
            else:
                accepted.append(candidate)

        sorted_issues = sorted(self.issues, key=lambda issue: issue["_sort"])
        return accepted, sorted_issues


# ---------------------------------------------------------------------
# Command-line execution
# ---------------------------------------------------------------------

def write_report(output_dir, report):
    path = output_dir / "extraction-report.json"
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def run(args):
    input_path = Path(args.input).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()

    # Validate input before touching old output files.
    if not input_path.is_file():
        LOGGER.error("Input PDF not found: %s", input_path)
        return 1
    if input_path.suffix.casefold() != ".pdf":
        LOGGER.error("Input file must have a .pdf extension.")
        return 1
    if any((output_dir / name).resolve() == input_path for name in OUTPUT_NAMES):
        LOGGER.error("Output directory would overwrite the input file.")
        return 1

    try:
        semester_map = load_semester_map(args.semester_map)
        supplied = {
            "academic_session": None,
            "semester": None,
            "course_code": None,
            "identifier_type": args.identifier_type,
            "attempt_number": args.attempt_number,
        }

        if args.academic_session:
            supplied["academic_session"] = normalize_session(args.academic_session)
            if supplied["academic_session"] is None:
                raise ValueError(
                    "--academic-session must be consecutive years, e.g. 2026/2027."
                )

        if args.semester:
            supplied["semester"] = normalize_semester(args.semester, semester_map)
            if supplied["semester"] is None:
                raise ValueError(
                    "--semester must map to harmattan or rain in the reviewed map."
                )

        if args.course_code:
            supplied["course_code"] = compact_course(args.course_code)
            if not re.fullmatch(r"[A-Za-z]{2,4}\d{3}[A-Za-z]?", supplied["course_code"]):
                raise ValueError(
                    "--course-code must resemble a code such as EEE301."
                )

        if args.attempt_number is not None and not 1 <= args.attempt_number <= 32767:
            raise ValueError("--attempt-number must be from 1 to 32767.")

        output_dir.mkdir(parents=True, exist_ok=True)
        # Remove old outputs only after the input and arguments have passed
        # basic validation, so a typo does not erase a previous run.
        for name in OUTPUT_NAMES:
            (output_dir / name).unlink(missing_ok=True)

        report = {
            "converter_version": VERSION,
            "status": "failed",
            "input_filename": input_path.name,
            "input_sha256": sha256_file(input_path),
        }

        converter = Converter(
            input_path.name, supplied, semester_map, args.table_strategy
        )

        with pdfplumber.open(str(input_path)) as pdf:
            total_pages = len(pdf.pages)
            if total_pages == 0:
                raise ValueError("PDF has no pages.")
            for page_number, page in enumerate(pdf.pages, start=1):
                converter.process_page(page, page_number)

        accepted, issues = converter.finish()
        results = [item["row"] for item in accepted]
        trace = [
            {
                "output_row_number": output_number,
                "source_file": input_path.name,
                "page": item["loc"][0],
                "table": item["loc"][1],
                "source_row": item["loc"][2],
                "source_credit_units": item["units"],
                "source_grade_label": item["grade"],
                "context_origin": item["origin"],
            }
            for output_number, item in enumerate(accepted, start=1)
        ]

        write_csv(output_dir / "results.csv", RESULT_HEADERS, results)
        write_csv(output_dir / "issues.csv", ISSUE_HEADERS, [
            {key: value for key, value in issue.items() if key != "_sort"}
            for issue in issues
        ])
        write_csv(output_dir / "trace.csv", TRACE_HEADERS, trace)

        unresolved_count = len(converter.unresolved_rows)
        counts_reconcile = (
            converter.source_rows == len(results) + unresolved_count
        )

        if not counts_reconcile:
            status = "failed_count_mismatch"
        elif not results:
            status = "no_rows_accepted"
        elif issues:
            status = "ok_with_issues"
        else:
            status = "ok"

        report.update({
            "status": status,
            "total_pages": total_pages,
            "source_data_row_count": converter.source_rows,
            "accepted_row_count": len(results),
            "unresolved_row_count": unresolved_count,
            "counts_reconcile": counts_reconcile,
            "issue_entry_count": len(issues),
            "excluded": {
                "header_rows": converter.header_rows,
                "rows_above_header": converter.heading_rows,
                "blank_rows": converter.blank_rows,
            },
            "pages_without_extractable_text": converter.pages_without_text,
            "pages_without_tables": converter.pages_without_tables,
            "table_strategy": args.table_strategy,
            "supplied_context": {
                key: value for key, value in supplied.items()
                if value is not None
            },
            "semester_mapping": semester_map,
            "diagnostic_counts": dict(sorted(Counter(
                issue["diagnostic_code"] for issue in issues
            ).items())),
            "warnings": [
                "results.csv contains accepted rows only; it may not be complete.",
                "Review every accepted row against the original PDF.",
                "Scanned pages are reported but are not OCR'd by this version.",
                *converter.warnings,
            ],
        })
        write_report(output_dir, report)

        print(f"Status: {status}")
        print(
            f"Source rows: {converter.source_rows} | "
            f"accepted: {len(results)} | unresolved: {unresolved_count}"
        )
        print(f"Outputs written to: {output_dir}")

        if not counts_reconcile:
            LOGGER.error("Counts do not reconcile. Do not use these outputs.")
            return 1
        return 0 if results else 2

    except Exception as error:
        # A clear report is attempted if an error happens after output setup.
        LOGGER.error("Conversion failed: %s: %s", type(error).__name__, error)
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            write_report(output_dir, {
                "converter_version": VERSION,
                "status": "failed",
                "input_filename": input_path.name,
                "input_sha256": None,
                "error": f"{type(error).__name__}: {error}",
            })
        except OSError:
            pass
        return 1


def build_parser():
    parser = argparse.ArgumentParser(
        description="Extract academic result rows from a text-based PDF into CSV."
    )
    parser.add_argument("input", help="Path to input PDF")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--academic-session", help="Reviewed session, e.g. 2026/2027")
    parser.add_argument("--semester", help="Reviewed semester: harmattan or rain")
    parser.add_argument("--course-code", help="Reviewed course code if absent from PDF")
    parser.add_argument(
        "--identifier-type",
        choices=["matriculation", "utme"],
        help="Supply if the PDF header does not establish the identifier type",
    )
    parser.add_argument(
        "--attempt-number", type=int,
        help="Reviewed attempt number if absent from the PDF",
    )
    parser.add_argument("--semester-map", help="JSON file mapping source labels to semesters")
    parser.add_argument(
        "--table-strategy", choices=["lines", "text"], default="lines",
        help="'lines' for ruled tables; 'text' for borderless tables",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        format="%(levelname)s: %(message)s",
        level=logging.INFO if args.verbose else logging.WARNING,
    )
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
