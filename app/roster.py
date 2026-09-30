"""
Parse a class roster into a clean list of student names.

Supported inputs:
  * Pasted text, one name per line.
  * Pasted text on a single line, names separated by commas.
  * An uploaded CSV file. If it has a header row, recognised columns are used:
      - "first"/"first name" + "last"/"last name"  -> "First Last"
      - "name"/"student"/"student name"/"full name" -> used as-is
    Without a recognised header, the first column is used (or all cells of a
    row joined with spaces when there are several columns).

Names are never stored; the parsed list is returned to the caller only.
"""

import csv
import io

MAX_NAME_LENGTH = 60
MAX_STUDENTS = 500

_FIRST_HEADERS = {"first", "first name", "firstname", "first_name", "given name"}
_LAST_HEADERS = {"last", "last name", "lastname", "last_name", "surname", "family name"}
_FULL_HEADERS = {"name", "student", "student name", "full name", "fullname", "student_name"}


class RosterError(ValueError):
    """Raised when a roster cannot be used (e.g. it is empty)."""


def _clean(name: str) -> str:
    """Collapse internal whitespace and strip surrounding spaces/quotes."""
    name = " ".join(name.replace("﻿", "").split())
    return name.strip(" \"'")


def _finish(names: list[str]) -> list[str]:
    """Drop blanks, validate lengths and count, and return the final list."""
    names = [n for n in (_clean(n) for n in names) if n]
    if not names:
        raise RosterError("No student names were found. Paste names or upload a CSV.")
    if len(names) > MAX_STUDENTS:
        raise RosterError(f"Too many students ({len(names)}). The limit is {MAX_STUDENTS}.")
    too_long = [n for n in names if len(n) > MAX_NAME_LENGTH]
    if too_long:
        raise RosterError(
            f"These names are longer than {MAX_NAME_LENGTH} characters: {', '.join(too_long[:3])}"
        )
    return names


def parse_pasted(text: str) -> list[str]:
    """Parse names typed or pasted into a text box."""
    text = (text or "").strip()
    if not text:
        raise RosterError("No student names were entered.")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) == 1:
        # A single line is treated as a comma-separated list.
        return _finish(lines[0].split(","))
    # Multiple lines: one student per line, used verbatim.
    return _finish(lines)


def parse_csv(text: str) -> list[str]:
    """Parse the text contents of an uploaded CSV file."""
    text = (text or "").lstrip("﻿").strip()
    if not text:
        raise RosterError("The uploaded CSV file is empty.")

    rows = [row for row in csv.reader(io.StringIO(text)) if any(c.strip() for c in row)]
    if not rows:
        raise RosterError("The uploaded CSV file has no names in it.")

    header = [c.strip().lower() for c in rows[0]]
    first_col = next((i for i, h in enumerate(header) if h in _FIRST_HEADERS), None)
    last_col = next((i for i, h in enumerate(header) if h in _LAST_HEADERS), None)
    full_col = next((i for i, h in enumerate(header) if h in _FULL_HEADERS), None)

    def cell(row: list[str], i: int) -> str:
        return row[i] if i < len(row) else ""

    if first_col is not None and last_col is not None:
        return _finish([f"{cell(r, first_col)} {cell(r, last_col)}" for r in rows[1:]])
    if full_col is not None:
        return _finish([cell(r, full_col) for r in rows[1:]])

    # No recognised header: every row is a student.
    return _finish([" ".join(c.strip() for c in r if c.strip()) for r in rows])
