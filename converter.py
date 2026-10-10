-"""Academic result PDF -> CSV converter (text PDFs and scanned PDFs).

Writes four files per run: results.csv, issues.csv, trace.csv and
extraction-report.json. It never touches a database and never guesses:
anything unclear goes to issues.csv instead of results.csv.

Text pages are read from ruled tables; scanned pages are OCR'd with
Tesseract (see ocr.py) and then validated by exactly the same rules.

Exit codes: 0 = rows accepted (issues.csv may still have entries),
            1 = hard failure (missing/corrupt/encrypted PDF, bad arguments),
            2 = ran, but no rows were accepted.
"""

import argparse
import csv
import difflib
import hashlib
import json
import logging
import re
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pdfplumber


VERSION = "0.2.0"
log = logging.getLogger("results_converter")

RESULT_HEADERS = [
    "identifier_type", "identifier_value", "course_code",
    "academic_session", "semester", "attempt_number", "score",
]
ISSUE_HEADERS = [
    "source_file", "page", "table", "source_row", "source_cell",
    "raw_value", "diagnostic_code", "explanation",
]
TRACE_HEADERS = [
    "output_row_number", "source_file", "page", "table", "source_row",
    "source_credit_units", "source_grade_label", "context_origin",
    "source_method", "ocr_min_confidence", "source_bbox",
]
OUTPUT_NAMES = ["results.csv", "issues.csv", "trace.csv",
                "extraction-report.json"]

# Only these labels are accepted by default. Anything else (for example "first semester") must be added through a reviewed --semester-map file.
DEFAULT_SEMESTER_MAP = {"harmattan": "harmattan", "rain": "rain"}
VALID_SEMESTERS = {"harmattan", "rain"}

CONTEXT_KEYS = ("academic_session", "semester", "course_code")
HEADER_SEARCH_ROWS = 4


FIELD_ALIASES = {
    "identifier": {
        "matric", "matricno", "matricnumber", "matriculation",
        "matriculationno", "matriculationnumber", "regno", "regnumber",
        "registrationno", "registrationnumber", "utme", "utmeno",
        "utmenumber", "jamb", "jambno", "jambnumber", "identifier",
        "studentid", "studentno",
    },
    "score": {"score", "totalscore", "total", "mark", "marks",
              "finalscore"},
    "course": {"course", "coursecode"},
    "session": {"session", "academicsession"},
    "semester": {"semester"},
    "attempt": {"attempt", "attemptno", "attemptnumber"},
    "units": {"unit", "units", "creditunit", "creditunits", "cu"},
    "grade": {"grade", "gradelabel"},
}



# --------------------------------------------------------------------------
# Small pure helpers (easy to unit test)
# --------------------------------------------------------------------------

def clean(value):
    """Collapse whitespace; None becomes an empty string."""
    return " ".join(str(value).split()) if value is not None else ""


def norm_header(value):
    return re.sub(r"[^a-z0-9]", "", clean(value).casefold())


def compact_course(value):
    """Remove whitespace inside a course code; case is preserved."""
    return re.sub(r"\s+", "", value or "")


def normalize_session(value):
    """Return 'YYYY/YYYY' for consecutive years, otherwise None."""
    match = SESSION_RE.search(clean(value))
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    return f"{start}/{end}" if end == start + 1 else None


def normalize_semester(value, semester_map):
    return semester_map.get(clean(value).casefold())


def parse_score(raw):
    """Return (score_text, None) or (None, (code, explanation))."""
    text = clean(raw)
    if text.casefold() in MISSING_SCORE_MARKERS:
        shown = text or "(blank)"
        return None, ("MISSING_SCORE",
                      f"Score is {shown}. Not converted to 0.00; "
                      "needs review.")
    if re.match(r"^-\d", text):
        return None, ("INVALID_SCORE", "Score is negative.")
    if not SCORE_RE.match(text):
        return None, ("UNREADABLE_SCORE",
                      "Score is not a plain number; possible OCR/extraction "
                      "problem. Not guessed.")
    value = Decimal(text)
    if value > 100:
        return None, ("INVALID_SCORE", "Score is above 100.")
    if -value.as_tuple().exponent > 2:
        return None, ("INVALID_SCORE",
                      "Score has more than two decimal places.")
    return f"{value.quantize(Decimal('0.01')):.2f}", None


def check_identifier(raw):
    """Return None if the identifier is safe to export, else (code, why)."""
    if not raw:
        return ("MISSING_IDENTIFIER", "Identifier cell is blank.")
    if re.search(r"\s", raw):
        return ("AMBIGUOUS_IDENTIFIER",
                "Identifier contains internal whitespace; it may be split "
                "or merged across cells. Not repaired automatically.")
    if SCI_NOTATION_RE.match(raw) or FLOAT_ZERO_RE.match(raw):
        return ("AMBIGUOUS_IDENTIFIER",
                "Identifier looks like a number that was converted "
                "(scientific notation or trailing .0). Check the source.")
    return None


def check_ocr_identifier(raw):
    """Extra identifier checks for OCR text. Never changes the value."""
    if raw and not re.search(r"\d", raw):
        return ("AMBIGUOUS_IDENTIFIER",
                "OCR identifier contains no digits, so it may not be a "
                "student identifier (for example a footer or heading).")
    if CONFUSABLE_RE.match(raw):
        return ("AMBIGUOUS_IDENTIFIER",
                "OCR identifier mixes digits with letters that OCR often "
                "confuses (O, I, l, S, B, Z). Not corrected automatically; "
                "check the source.")
    return None


def parse_attempt(raw):
    """Return (int, None) or (None, (code, explanation))."""
    text = clean(raw)
    if not ATTEMPT_RE.match(text) or not 1 <= int(text) <= 32767:
        return None, ("INVALID_ATTEMPT",
                      "Attempt number must be an integer from 1 to 32767.")
    return int(text), None


def match_header_label(label, fuzzy, fuzzy_log):
    """Return (field, alias) for a normalized header label, or (None, None).

    With fuzzy=True (OCR only) a label that is close to a known header
    name is accepted, because OCR often slips by one letter ("Matric
    Nc"). Only header names are matched this way, never data values, and
    each fuzzy match is appended to fuzzy_log so it is reported.
    """
    if label in ALL_ALIASES:
        return ALL_ALIASES[label], label
    if fuzzy and len(label) >= 4:
        close = difflib.get_close_matches(label, sorted(ALL_ALIASES), n=1,
                                          cutoff=FUZZY_CUTOFF)
        if close:
            if fuzzy_log is not None:
                fuzzy_log.append((label, close[0]))
            return ALL_ALIASES[close[0]], close[0]
    return None, None


def detect_header(row, fuzzy=False, fuzzy_log=None):
    """Return (mapping, identifier_type_hint, error).

    mapping is {field: column_index} when the row is a header row (it
    must contain an identifier and a score column), otherwise None.
    """
    mapping, hint = {}, None
    matches = []
    for idx, cell in enumerate(row):
        label = norm_header(cell)
        if not label:
            continue
        field, alias = match_header_label(label, fuzzy, matches)
        if field is None:
            continue
        if field in mapping:
            return None, None, f"Header has more than one '{field}' column."
        mapping[field] = idx
        if field == "identifier":
            if "matric" in alias or alias.startswith("reg"):
                hint = "matriculation"
            elif "utme" in alias or "jamb" in alias:
                hint = "utme"
    if "identifier" in mapping and "score" in mapping:
        if fuzzy_log is not None:
            fuzzy_log.extend(matches)
        return mapping, hint, None
    return None, None, None


def scan_heading_lines(lines, semester_map):
    """Find session / semester / course values in heading text."""
    values = {key: set() for key in CONTEXT_KEYS}
    invalid = {key: [] for key in CONTEXT_KEYS}
    labels = sorted(semester_map, key=len, reverse=True)

    for line in lines:
        text = clean(line)
        if not text:
            continue

        for match in SESSION_RE.finditer(text):
            session = normalize_session(match.group(0))
            if session:
                values["academic_session"].add(session)
            else:
                invalid["academic_session"].append(match.group(0))

        remaining = text.casefold()
        for label in labels:
            pattern = r"(?<!\w)" + re.escape(label) + r"(?!\w)"
            if re.search(pattern, remaining):
                values["semester"].add(semester_map[label])
                remaining = re.sub(pattern, " ", remaining)

        match = COURSE_HEADING_RE.match(text)
        if match:
            code = compact_course(match.group(1))
            prefix = re.match(r"[A-Za-z]+", code).group(0).upper()
            if prefix not in COURSE_STOP_PREFIXES:
                values["course_code"].add(code)

    return values, invalid


def resolve(key, supplied, found, column):
    """Combine supplied / heading / column values for one row.

    Returns (value, origin, None) or (None, None, (code, explanation)).
    """
    if found and found.get("problem"):
        return None, None, found["problem"]

    candidates = []
    if supplied is not None:
        candidates.append((supplied, "supplied"))
    if found and found.get("value") is not None:
        candidates.append((found["value"], found["origin"]))
    if column is not None:
        candidates.append((column, "column"))

    if not candidates:
        return None, None, (
            "MISSING_CONTEXT",
            f"{key} was not found in the PDF and was not supplied.")
    if len({value for value, _ in candidates}) > 1:
        detail = "; ".join(f"{v} ({o})" for v, o in candidates)
        return None, None, ("CONFLICTING_CONTEXT",
                            f"{key} disagrees between sources: {detail}.")
    return candidates[0][0], "+".join(o for _, o in candidates), None


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_csv(path, headers, rows):
    """Always write CSV through the csv module, never by joining strings."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers,
                                extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def load_semester_map(path):
    mapping = dict(DEFAULT_SEMESTER_MAP)
    if path:
        extra = json.loads(Path(path).read_text(encoding="utf-8"))
        for label, semester in extra.items():
            if semester not in VALID_SEMESTERS:
                raise ValueError(
                    f"Semester map value '{semester}' must be "
                    "harmattan or rain.")
            mapping[clean(label).casefold()] = semester
    return mapping


# --------------------------------------------------------------------------
# The extraction run
# --------------------------------------------------------------------------

class Converter:
    def __init__(self, source_file, supplied, semester_map, strategy,
                 ocr_config):
        self.source_file = source_file
        self.supplied = supplied
        self.semester_map = semester_map
        self.table_settings = {"vertical_strategy": strategy,
                               "horizontal_strategy": strategy}
        self.ocr = ocr_config

        self.candidates = []
        self.issues = []
        self.unresolved = set()
        self.source_rows = 0
        self.blank_rows = 0
        self.header_rows = 0
        self.heading_rows = 0

        self.mapping = None
        self.ncols = None
        self.hint = None
        self.found = {key: None for key in CONTEXT_KEYS}

        self.engine = None
        self.engine_version = None
        self.engine_error = None
        self.ocr_layout = None  # (column bounds, page size) of last OCR page
        self.ocr_pages = {}
        self.page_methods = {}
        self.pages_without_text = []
        self.pages_without_tables = []
        self.carried = {key: [] for key in CONTEXT_KEYS}
        self.warnings = []

    # ---- bookkeeping ----------------------------------------------------

    def add_issue(self, page, table, row, cell, raw, code, explanation,
                  counts_as_row=True):
        self.issues.append({
            "source_file": self.source_file, "page": page,
            "table": table or "", "source_row": row or "",
            "source_cell": cell, "raw_value": raw,
            "diagnostic_code": code, "explanation": explanation,
            "_sort": (page, table or 0, row or 0, len(self.issues)),
        })
        if counts_as_row:
            self.unresolved.add((page, table, row))

    def page_issue(self, page_no, code, explanation):
        self.add_issue(page_no, None, None, "page", "", code, explanation,
                       counts_as_row=False)

    # ---- page level -----------------------------------------------------

    def process_page(self, page, page_no):
        mode = self.ocr.mode
        tables = [] if mode == "force" else \
            page.find_tables(self.table_settings)

        wants_ocr = mode == "force" or (
            mode == "auto" and (not page.chars
                                or (not tables and page.images)))
        if wants_ocr:
            self.process_ocr_page(page, page_no)
            return

        if not page.chars and not tables:
            self.pages_without_text.append(page_no)
            self.page_methods[page_no] = "none"
            self.page_issue(
                page_no, "UNREADABLE_PAGE",
                "No extractable text. Likely a scanned page; run with "
                "OCR enabled (--ocr auto) or check the PDF.")
            return
        if not tables:
            self.pages_without_tables.append(page_no)
            self.page_methods[page_no] = "text"
            self.page_issue(
                page_no, "UNSUPPORTED_LAYOUT",
                "Text found but no ruled table detected. Layout not "
                "supported; no rows were extracted from this page.")
            return

        self.page_methods[page_no] = "text"
        heading_lines = self.outside_text(page, tables).splitlines()
        prepared = []
        for number, table in enumerate(tables, start=1):
            rows = [[clean(c) for c in row] for row in table.extract()]
            prep = self.prepare_rows(number, rows, page_no,
                                     HEADER_SEARCH_ROWS)
            heading_lines.extend(prep["heading_lines"])
            prepared.append(prep)

        self.update_context(heading_lines, page_no)
        for prep in prepared:
            self.process_table(prep, page_no)

    @staticmethod
    def outside_text(page, tables):
        boxes = [t.bbox for t in tables]

        def keep(obj):
            if obj.get("object_type") != "char":
                return True
            cx = (obj["x0"] + obj["x1"]) / 2
            cy = (obj["top"] + obj["bottom"]) / 2
            return not any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3]
                           for b in boxes)

        return page.filter(keep).extract_text() or ""

    def update_context(self, heading_lines, page_no):
        values, invalid = scan_heading_lines(heading_lines,
                                             self.semester_map)
        for key in CONTEXT_KEYS:
            origin = f"heading p{page_no}"
            if invalid[key]:
                self.found[key] = {
                    "value": None, "origin": origin,
                    "problem": ("INVALID_CONTEXT",
                                f"Heading {key} is not valid: "
                                f"{', '.join(invalid[key])}.")}
            elif len(values[key]) > 1:
                self.found[key] = {
                    "value": None, "origin": origin,
                    "problem": ("AMBIGUOUS_CONTEXT",
                                f"Several different {key} values appear "
                                f"on page {page_no}: "
                                f"{', '.join(sorted(values[key]))}.")}
            elif values[key]:
                self.found[key] = {"value": next(iter(values[key])),
                                   "origin": origin, "problem": None}
            elif self.found[key] is not None:
                self.carried[key].append(page_no)

    # ---- OCR pages ------------------------------------------------------

    def get_engine(self):
        if self.engine_error:
            raise ocr.OcrUnavailable(self.engine_error)
        if self.engine is None:
            try:
                self.engine, self.engine_version = ocr.load_engine()
            except ocr.OcrUnavailable as error:
                self.engine_error = str(error)
                raise
        return self.engine

    def is_heading_line(self, texts):
        """A short line that carries session/semester/course context."""
        filled = [t for t in texts if t]
        if not filled or len(filled) >= 3:
            return False
        values, invalid = scan_heading_lines([" ".join(filled)],
                                             self.semester_map)
        return any(values.values()) or any(invalid.values())

    def process_ocr_page(self, page, page_no):
        self.page_methods[page_no] = "ocr"
        cfg = self.ocr
        try:
            engine = self.get_engine()
            ocr_page = ocr.read_page(engine, page, cfg)
        except ocr.OcrUnavailable as error:
            self.page_methods[page_no] = "none"
            self.pages_without_text.append(page_no)
            self.page_issue(page_no, "UNREADABLE_PAGE",
                            f"Page needs OCR but OCR is unavailable: {error}")
            return
        except Exception as error:  # noqa: BLE001
            self.page_methods[page_no] = "none"
            self.pages_without_text.append(page_no)
            self.page_issue(page_no, "UNREADABLE_PAGE",
                            f"OCR failed on this page: {error}")
            return

        self.ocr_pages[page_no] = {
            "rotation_degrees": ocr_page.rotation,
            "skew_degrees": ocr_page.skew,
            "lines": len(ocr_page.lines),
            "mean_confidence": ocr_page.mean_confidence,
        }
        if not ocr_page.lines:
            self.pages_without_text.append(page_no)
            self.page_issue(page_no, "UNREADABLE_PAGE",
                            "OCR found no text on this page.")
            return
        if ocr_page.rotation:
            self.warnings.append(
                f"Page {page_no}: rotated {ocr_page.rotation} degrees "
                "before OCR.")

        lines, gap = ocr_page.lines, ocr_page.gap

        # 1. Find the header line using gap-separated cells.
        header_idx, header_groups = None, None
        for i, line in enumerate(lines[:OCR_HEADER_SEARCH_LINES]):
            groups = ocr.group_cells(line, gap)
            mapping, _, _ = detect_header([g[0] for g in groups],
                                          fuzzy=True)
            if mapping:
                header_idx, header_groups = i, groups
                break

        # 2. Work out the column boundaries.
        table_lines = lines[header_idx:] if header_idx is not None else lines
        bounds = ocr.find_column_bounds(table_lines, gap)
        page_size = (round(page.width), round(page.height))
        if header_idx is not None:
            if len(bounds) + 1 != len(header_groups):
                bounds = ocr.header_bounds(header_groups)
                self.warnings.append(
                    f"Page {page_no} (OCR): column boundaries were "
                    "estimated from the header positions.")
            self.ocr_layout = (bounds, page_size)
        else:
            previous = self.ocr_layout
            if not previous or previous[1] != page_size:
                self.page_issue(
                    page_no, "UNSUPPORTED_LAYOUT",
                    "OCR found text but no header row, and there is no "
                    "earlier scanned page with the same layout to "
                    "continue from. No rows were extracted.")
                return
            if len(bounds) != len(previous[0]):
                bounds = previous[0]
                self.warnings.append(
                    f"Page {page_no} (OCR): reused the column boundaries "
                    "of the previous scanned page.")

        # 3. Build rows of cells (plus confidence and position per line).
        rows, meta, skip = [], {}, set()
        for i, line in enumerate(lines):
            texts, confs = ocr.assign_cells(line, bounds)
            rows.append(texts)
            meta[i] = {
                "conf": {c: v for c, v in enumerate(confs)
                         if v is not None},
                "bbox": ",".join(str(v) for v in ocr.line_bbox(line)),
            }
            after_header = header_idx is None or i > header_idx
            if after_header and self.is_heading_line(texts):
                skip.add(i)

        prep = self.prepare_rows(1, rows, page_no, OCR_HEADER_SEARCH_LINES,
                                 fuzzy=True)
        prep["meta"], prep["skip"] = meta, skip
        heading_lines = prep["heading_lines"] + [
            " ".join(t for t in rows[i] if t) for i in sorted(skip)]
        self.update_context(heading_lines, page_no)

        if cfg.review_dir:
            ocr.save_review_image(
                ocr_page, bounds, cfg.min_confidence,
                Path(cfg.review_dir) / f"page-{page_no:04d}.png")

        self.process_table(prep, page_no)

    # ---- table level ----------------------------------------------------

    def prepare_rows(self, number, rows, page_no, search, fuzzy=False):
        """Find the header, decide the column mapping, collect headings."""
        prep = {"number": number, "rows": rows, "mapping": None,
                "hint": None, "error": None, "data_start": 0,
                "heading_lines": [], "meta": {}, "skip": set(),
                "fuzzy": fuzzy}

        header_idx, error, fuzzy_log = None, None, []
        for i, row in enumerate(rows[:search]):
            mapping, hint, error = detect_header(row, fuzzy, fuzzy_log)
            if error:
                break
            if mapping:
                header_idx = i
                prep.update(mapping=mapping, hint=hint)
                break
        for label, alias in fuzzy_log:
            if label != alias:
                self.warnings.append(
                    f"Page {page_no} (OCR): header label '{label}' was "
                    f"read as '{alias}'. Check the header against the "
                    "source.")

        if header_idx is not None:
            for row in rows[:header_idx]:
                text = " ".join(c for c in row if c)
                if text:
                    prep["heading_lines"].append(text)
                    self.heading_rows += 1
            self.header_rows += 1
            prep["data_start"] = header_idx + 1
            if self.mapping and self.mapping != prep["mapping"]:
                self.warnings.append(
                    f"Column layout changed at page {page_no}, "
                    f"table {number}.")
            self.mapping, self.hint = prep["mapping"], prep["hint"]
            self.ncols = len(rows[0]) if rows else None
        elif (not error and self.mapping and rows
              and len(rows[0]) == self.ncols):
            # Continuation of a table that started on an earlier page.
            prep.update(mapping=self.mapping, hint=self.hint)
            self.warnings.append(
                f"Page {page_no}, table {number}: no header row; "
                "reused the column mapping from the previous page.")
        else:
            prep["error"] = error or (
                "No header row with an identifier and a score column "
                "was found, and there is no earlier table to continue.")
        return prep

    def process_table(self, prep, page_no):
        number, rows = prep["number"], prep["rows"]

        if prep["error"]:
            for i, row in enumerate(rows):
                if any(row):
                    self.source_rows += 1
                    self.add_issue(page_no, number, i + 1, "row",
                                   " | ".join(row), "UNSUPPORTED_LAYOUT",
                                   prep["error"])
            return

        mapping, hint = prep["mapping"], prep["hint"]
        for i in range(prep["data_start"], len(rows)):
            row, row_no = rows[i], i + 1
            if i in prep["skip"]:
                self.heading_rows += 1
                continue
            if not any(row):
                self.blank_rows += 1
                continue
            repeated, new_hint, _ = detect_header(row, prep["fuzzy"])
            if repeated:
                self.header_rows += 1
                if repeated != mapping:
                    self.warnings.append(
                        f"Column layout changed at page {page_no}, "
                        f"table {number}, row {row_no}.")
                    mapping, hint = repeated, new_hint
                    self.mapping, self.hint = mapping, hint
                continue
            self.handle_row(page_no, number, row_no, row, mapping, hint,
                            prep["meta"].get(i))

    # ---- row level ------------------------------------------------------

    def handle_row(self, page_no, table_no, row_no, cells, mapping, hint,
                   meta=None):
        self.source_rows += 1
        raw_row = " | ".join(cells)
        problems = []

        def flush():
            for cell, raw, code, why in problems:
                self.add_issue(page_no, table_no, row_no, cell, raw,
                               code, why)

        if max(mapping.values()) >= len(cells):
            problems.append(("row", raw_row, "UNSUPPORTED_LAYOUT",
                             "Row has fewer cells than the header."))
            return flush()

        def get(field):
            return cells[mapping[field]] if field in mapping else ""

        identifier, score_raw = get("identifier"), get("score")
        if not identifier and not score_raw:
            problems.append((
                "row", raw_row, "SPLIT_ROW",
                "Identifier and score are both blank; this may be the "
                "continuation of a split row. Not merged automatically."))
            return flush()

        id_problem = check_identifier(identifier)
        if not id_problem and meta is not None:
            id_problem = check_ocr_identifier(identifier)
        if id_problem:
            problems.append(("identifier", identifier, *id_problem))

        score, score_problem = parse_score(score_raw)
        if score_problem:
            problems.append(("score", score_raw, *score_problem))

        # OCR confidence: low confidence sends the row to review. High
        # confidence alone never proves a value is right.
        min_conf = None
        if meta is not None:
            for field in OCR_CHECKED_FIELDS:
                if field not in mapping or not get(field):
                    continue
                conf = meta["conf"].get(mapping[field])
                if conf is None:
                    continue
                min_conf = conf if min_conf is None else min(min_conf, conf)
                if conf < self.ocr.min_confidence:
                    problems.append((
                        field, get(field), "LOW_CONFIDENCE_OCR",
                        f"OCR confidence {conf:.0f} is below the "
                        f"threshold {self.ocr.min_confidence:.0f}. "
                        "Check the source."))

        # Optional columns -------------------------------------------------
        col_session = col_semester = col_course = col_attempt = None

        raw = get("session")
        if raw:
            col_session = normalize_session(raw)
            if col_session is None:
                problems.append(("session", raw, "INVALID_CONTEXT",
                                 "Session must be consecutive years."))
        raw = get("semester")
        if raw:
            col_semester = normalize_semester(raw, self.semester_map)
            if col_semester is None:
                problems.append(("semester", raw, "INVALID_CONTEXT",
                                 "Semester label has no reviewed mapping."))
        raw = get("course")
        if raw:
            col_course = compact_course(raw)
        raw = get("attempt")
        if raw:
            attempt, attempt_problem = parse_attempt(raw)
            if attempt_problem:
                problems.append(("attempt", raw, *attempt_problem))
            else:
                col_attempt = str(attempt)

        # Context ------------------------------------------------------------
        supplied = self.supplied
        resolved, origins = {}, {}
        checks = [
            ("academic_session", supplied["academic_session"],
             self.found["academic_session"], col_session),
            ("semester", supplied["semester"],
             self.found["semester"], col_semester),
            ("course_code", supplied["course_code"],
             self.found["course_code"], col_course),
            ("identifier_type", supplied["identifier_type"],
             {"value": hint, "origin": "header", "problem": None}
             if hint else None, None),
            ("attempt_number", supplied["attempt_number"], None,
             col_attempt),
        ]
        for key, sup, found, col in checks:
            value, origin, problem = resolve(key, sup, found, col)
            if problem:
                problems.append((key, "", *problem))
            else:
                resolved[key], origins[key] = value, origin

        if problems:
            return flush()

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
            "units": get("units"), "grade": get("grade"),
            "origin": ";".join(f"{k}={origins[k]}" for k in origins),
            "method": "ocr" if meta is not None else "text",
            "min_conf": "" if min_conf is None else f"{min_conf:.1f}",
            "bbox": meta["bbox"] if meta is not None else "",
        })

    # ---- finish ---------------------------------------------------------

    def finish(self):
        """Flag duplicates and return (accepted, issues)."""
        keys = Counter(
            tuple(c["row"][h] for h in RESULT_HEADERS[:6])
            for c in self.candidates)
        accepted = []
        for cand in self.candidates:
            key = tuple(cand["row"][h] for h in RESULT_HEADERS[:6])
            if keys[key] > 1:
                page, table, row = cand["loc"]
                self.add_issue(
                    page, table, row, "row", cand["row"]["identifier_value"],
                    "DUPLICATE_ROW",
                    f"Same student/course/session/semester/attempt key "
                    f"appears {keys[key]} times. All copies are held back "
                    "for review; no attempt number was invented.")
            else:
                accepted.append(cand)
        issues = sorted(self.issues, key=lambda i: i["_sort"])
        return accepted, issues


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def write_report(output_dir, report):
    (output_dir / "extraction-report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")


def run(args):
    input_path = Path(args.input).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    review_dir = output_dir / "ocr-review"

    # Remove stale outputs so an old run can never pass for this one.
    for name in OUTPUT_NAMES:
        if output_dir / name == input_path:
            raise ValueError("The output directory would overwrite the "
                             "input file.")
    for name in OUTPUT_NAMES:
        (output_dir / name).unlink(missing_ok=True)
    for stale in review_dir.glob("page-*.png"):
        stale.unlink()

    report = {"converter_version": VERSION, "status": "failed",
              "input_filename": input_path.name, "input_sha256": None}

    try:
        if not input_path.is_file():
            raise FileNotFoundError(f"Input PDF not found: {input_path}")
        report["input_sha256"] = sha256_file(input_path)

        semester_map = load_semester_map(args.semester_map)
        supplied = {
            "academic_session": None, "semester": None,
            "course_code": None, "identifier_type": args.identifier_type,
            "attempt_number": None,
        }
        if args.academic_session:
            supplied["academic_session"] = normalize_session(
                args.academic_session)
            if not supplied["academic_session"]:
                raise ValueError("--academic-session must be consecutive "
                                 "years, e.g. 2026/2027.")
        if args.semester:
            supplied["semester"] = normalize_semester(args.semester,
                                                      semester_map)
            if not supplied["semester"]:
                raise ValueError("--semester has no reviewed mapping to "
                                 "harmattan or rain.")
        if args.course_code:
            supplied["course_code"] = compact_course(args.course_code)
        if args.attempt_number is not None:
            if not 1 <= args.attempt_number <= 32767:
                raise ValueError("--attempt-number must be 1..32767.")
            supplied["attempt_number"] = str(args.attempt_number)

        ocr_config = ocr.OcrConfig(
            mode=args.ocr, dpi=args.ocr_dpi, lang=args.ocr_lang,
            psm=args.ocr_psm, min_confidence=args.ocr_min_confidence,
            deskew=not args.no_ocr_deskew,
            review_dir=review_dir if args.ocr_review_images else None)

        conv = Converter(input_path.name, supplied, semester_map,
                         args.table_strategy, ocr_config)
        with pdfplumber.open(str(input_path)) as pdf:
            total_pages = len(pdf.pages)
            if total_pages == 0:
                raise ValueError("The PDF has no pages.")
            for page_no, page in enumerate(pdf.pages, start=1):
                conv.process_page(page, page_no)
        accepted, issues = conv.finish()

    except Exception as error:  # noqa: BLE001 - report every failure
        report["error"] = f"{type(error).__name__}: {error}"
        write_report(output_dir, report)
        log.error("Conversion failed: %s", report["error"])
        return 1

    results = [c["row"] for c in accepted]
    trace = [{
        "output_row_number": n, "source_file": input_path.name,
        "page": c["loc"][0], "table": c["loc"][1],
        "source_row": c["loc"][2], "source_credit_units": c["units"],
        "source_grade_label": c["grade"], "context_origin": c["origin"],
        "source_method": c["method"], "ocr_min_confidence": c["min_conf"],
        "source_bbox": c["bbox"],
    } for n, c in enumerate(accepted, start=1)]

    write_csv(output_dir / "results.csv", RESULT_HEADERS, results)
    write_csv(output_dir / "issues.csv", ISSUE_HEADERS, issues)
    write_csv(output_dir / "trace.csv", TRACE_HEADERS, trace)

    unresolved = len(conv.unresolved)
    reconciles = conv.source_rows == len(results) + unresolved
    ocr_accepted = sum(1 for c in accepted if c["method"] == "ocr")
    if not results:
        status = "no_rows_accepted"
    elif issues:
        status = "ok_with_issues"
    else:
        status = "ok"
    if not reconciles:
        status = "failed_count_mismatch"

    warnings = list(conv.warnings)
    if ocr_accepted:
        warnings.append(
            f"{ocr_accepted} accepted row(s) come from OCR. OCR confidence "
            "does not prove correctness: compare every OCR row with the "
            "source page (use --ocr-review-images) before import.")
    warnings += [
        "results.csv is an accepted subset, not a complete history.",
        "Review every accepted row against the source PDF.",
    ]

    report.update({
        "status": status,
        "total_pages": total_pages,
        "page_methods": {str(k): v for k, v in
                         sorted(conv.page_methods.items())},
        "source_data_row_count": conv.source_rows,
        "accepted_row_count": len(results),
        "accepted_from_ocr_count": ocr_accepted,
        "unresolved_row_count": unresolved,
        "counts_reconcile": reconciles,
        "issue_entry_count": len(issues),
        "excluded": {
            "header_rows": conv.header_rows,
            "heading_rows": conv.heading_rows,
            "blank_rows": conv.blank_rows,
        },
        "pages_without_extractable_text": conv.pages_without_text,
        "pages_without_tables": conv.pages_without_tables,
        "table_strategy": args.table_strategy,
        "ocr": {
            "mode": args.ocr,
            "engine": conv.engine_version,
            "engine_error": conv.engine_error,
            "dpi": args.ocr_dpi, "lang": args.ocr_lang,
            "psm": args.ocr_psm,
            "min_confidence": args.ocr_min_confidence,
            "deskew": not args.no_ocr_deskew,
            "review_images": bool(args.ocr_review_images),
            "pages": {str(k): v for k, v in
                      sorted(conv.ocr_pages.items())},
        },
        "supplied_context": {k: v for k, v in supplied.items() if v},
        "semester_mapping": semester_map,
        "context_carried_over_pages": {
            k: v for k, v in conv.carried.items() if v},
        "diagnostic_counts": dict(sorted(Counter(
            i["diagnostic_code"] for i in issues).items())),
        "warnings": warnings,
    })
    write_report(output_dir, report)

    print(f"Status: {status}")
    print(f"Source rows: {conv.source_rows} | accepted: {len(results)} | "
          f"unresolved: {unresolved}")
    print(f"Outputs written to: {output_dir}")

    if not reconciles:
        log.error("Row counts do not reconcile; do not use these outputs.")
        return 1
    return 0 if results else 2


def build_parser():
    parser = argparse.ArgumentParser(
        description="Extract academic result rows from a PDF (text or "
                    "scanned) into the import CSV contract.")
    parser.add_argument("input", help="Path to the input PDF")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--academic-session",
                        help="Reviewed context, e.g. 2026/2027")
    parser.add_argument("--semester",
                        help="Reviewed context: harmattan or rain (or a "
                             "label from --semester-map)")
    parser.add_argument("--course-code",
                        help="Reviewed context when the PDF has no course "
                             "heading or column")
    parser.add_argument("--identifier-type",
                        choices=["matriculation", "utme"],
                        help="Only needed when the header does not say")
    parser.add_argument("--attempt-number", type=int,
                        help="Reviewed attempt number when the PDF has "
                             "no attempt column")
    parser.add_argument("--semester-map",
                        help="JSON file of reviewed label -> semester "
                             "mappings")
    parser.add_argument("--table-strategy", choices=["lines", "text"],
                        default="lines",
                        help="Text pages: 'lines' needs ruled tables "
                             "(default); 'text' is for borderless tables")

    group = parser.add_argument_group("OCR (scanned pages)")
    group.add_argument("--ocr", choices=["auto", "off", "force"],
                       default="auto",
                       help="auto: OCR pages with no text layer (default); "
                            "off: never OCR; force: OCR every page")
    group.add_argument("--ocr-dpi", type=int, default=300)
    group.add_argument("--ocr-lang", default="eng",
                       help="Tesseract language code (default: eng)")
    group.add_argument("--ocr-psm", type=int, default=6,
                       help="Tesseract page segmentation mode (default: 6)")
    group.add_argument("--ocr-min-confidence", type=float, default=85.0,
                       help="OCR cells below this confidence (0-100) go to "
                            "issues.csv (default: 85)")
    group.add_argument("--no-ocr-deskew", action="store_true",
                       help="Do not straighten slightly tilted scans")
    group.add_argument("--ocr-review-images", action="store_true",
                       help="Write annotated page images to "
                            "<output-dir>/ocr-review/ for checking rows. "
                            "These contain student data; keep them private.")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, format="%(levelname)s: %(message)s",
                        level=logging.INFO if args.verbose
                        else logging.WARNING)
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
