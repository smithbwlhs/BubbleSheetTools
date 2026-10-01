"""
Read a results.csv that this site produced, so a teacher can rebuild the
analysis later (lost ZIP, late students, or combining class periods).

Expected layout (see export.results_csv):

    Student, Class, Version, Sheet ID, MC correct, ..., Written 1.., Total, Needs review, Notes, Q1..Qn
    EXAM INFO, exam=Unit 4 Test, questions=20, choices=4, versions=2, written=2.0;7.0, ...
    ANSWER KEY V1, ..., B, AC, D, ...      (just "ANSWER KEY" on single-version exams)
    ANSWER KEY V2, ..., D, B, A, ...
    Ada Lovelace, Bio P3, 1, 3f9c01ab-01, ..., B, A?, D, ...

Open-response questions appear as "Qn (open)" columns holding the teacher's
score; the ANSWER KEY row has "open" there.

Rows are found by their first cell, not their position, so a file that was
opened in Excel, sorted or trimmed still loads. Score columns are ignored and
recalculated. Older results files without the Class / Sheet ID columns or the
EXAM INFO row still load; missing details are inferred and a warning is shown.

Parsing happens in memory; the uploaded file is never stored.
"""

import csv
import io
import re
from dataclasses import dataclass, field

from .answer_key import AnswerKeyError
from .sheet_layout import CHOICE_LETTERS, MAX_CHOICES, MAX_QUESTIONS, MAX_VERSIONS, MIN_CHOICES

MAX_ROWS = 2000


@dataclass
class ImportedStudent:
    name: str
    class_name: str
    exam_id: str
    roster_index: int | None
    version: int | None                         # None = still unknown ("?")
    answers: dict[int, frozenset[int]] | None  # None = bubble page never scanned
    uncertain: set[int]                         # answers marked "?" (still to check)
    written: dict[int, float | None]
    open_scores: dict[int, float | None] = field(default_factory=dict)


@dataclass
class ImportedResults:
    exam_name: str
    num_questions: int
    num_choices: int
    written_heights: tuple[float, ...]
    multi_mode: str
    keys: dict[int, dict[int, frozenset[int]]]  # version -> question -> correct choices
    num_versions: int
    exam_ids: set[str]
    open_questions: list[int] = field(default_factory=list)
    students: list[ImportedStudent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _letters(cell: str, where: str) -> frozenset[int]:
    out = set()
    for ch in cell.upper():
        if ch in " ,;":
            continue
        if ch not in CHOICE_LETTERS:
            raise AnswerKeyError(f"{where}: '{cell}' is not a valid answer.")
        out.add(CHOICE_LETTERS.index(ch))
    return frozenset(out)


def _number(cell: str, where: str) -> float | None:
    cell = cell.strip()
    if not cell:
        return None
    try:
        value = float(cell)
    except ValueError:
        raise AnswerKeyError(f"{where}: '{cell}' is not a number.") from None
    if not 0 <= value <= 1000:
        raise AnswerKeyError(f"{where}: {value} is out of range.")
    return value


def _parse_sheet_id(cell: str) -> tuple[str, int | None]:
    """"3f9c01ab-07" -> ("3f9c01ab", 7)."""
    m = re.fullmatch(r"\s*([0-9a-fA-F]{4,32})-(\d{1,4})\s*", cell or "")
    return (m.group(1).lower(), int(m.group(2))) if m else ("", None)


def parse_results_csv(filename: str, text: str) -> ImportedResults:
    """Parse a results CSV. Raises AnswerKeyError with a teacher-friendly message."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if len(rows) > MAX_ROWS:
        raise AnswerKeyError(f"{filename} has too many rows ({len(rows)}).")

    # ---- header ----
    header_i = next((i for i, r in enumerate(rows) if r and r[0].strip().lower() == "student"), None)
    if header_i is None:
        raise AnswerKeyError(
            f"{filename} doesn't look like a results CSV from this site "
            "(no 'Student' header row). Upload the results.csv you downloaded after grading.")
    header = [h.strip() for h in rows[header_i]]
    col = {h.lower(): i for i, h in enumerate(header)}
    q_cols = {int(m.group(1)): i for i, h in enumerate(header)
              if (m := re.fullmatch(r"Q(\d+)", h, re.IGNORECASE))}
    open_cols = {int(m.group(1)): i for i, h in enumerate(header)
                 if (m := re.fullmatch(r"Q(\d+)\s*\(open\)", h, re.IGNORECASE))}
    w_cols = {int(m.group(1)): i for i, h in enumerate(header)
              if (m := re.fullmatch(r"Written (\d+)", h, re.IGNORECASE))}
    if not q_cols:
        raise AnswerKeyError(f"{filename} has no question columns (Q1, Q2, ...).")
    num_q = max(set(q_cols) | set(open_cols))
    if sorted(set(q_cols) | set(open_cols)) != list(range(1, num_q + 1)) or num_q > MAX_QUESTIONS:
        raise AnswerKeyError(f"{filename}: the question columns should run Q1 to Q{num_q} "
                             "with none missing.")

    def cell(row: list[str], i: int | None) -> str:
        return row[i].strip() if i is not None and i < len(row) else ""

    body = [r for r in rows[header_i + 1:] if any(c.strip() for c in r)]

    # ---- EXAM INFO (key=value cells) ----
    info: dict[str, str] = {}
    for r in body:
        if r[0].strip().upper() == "EXAM INFO":
            for c in r[1:]:
                if "=" in c:
                    k, v = c.split("=", 1)
                    info[k.strip().lower()] = v.strip()

    # ---- ANSWER KEY row(s): "ANSWER KEY", or "ANSWER KEY V1", "ANSWER KEY V2", ... ----
    keys: dict[int, dict[int, frozenset[int]]] = {}
    for r in body:
        m = re.fullmatch(r"ANSWER KEY(?:\s*(?:V|VERSION)\s*(\d))?", r[0].strip().upper())
        if not m:
            continue
        version = int(m.group(1) or 1)
        if not 1 <= version <= MAX_VERSIONS or version in keys:
            raise AnswerKeyError(f"{filename}: unexpected answer key row '{r[0].strip()}'.")
        label = f"version {version} answer key" if m.group(1) else "answer key"
        key = {}
        for q, i in q_cols.items():
            answer = _letters(cell(r, i), f"Answer key Q{q}")
            if not answer:
                raise AnswerKeyError(f"{filename}: the {label} has no answer for Q{q}.")
            key[q] = answer
        keys[version] = key
    if not keys:
        raise AnswerKeyError(f"{filename} is missing its ANSWER KEY row.")
    try:
        num_versions = int(info.get("versions", 0)) or max(keys)
    except ValueError:
        num_versions = max(keys)
    missing = [v for v in range(1, num_versions + 1) if v not in keys]
    if missing or not 1 <= num_versions <= MAX_VERSIONS:
        raise AnswerKeyError(f"{filename} is missing the answer key for version "
                             f"{', '.join(map(str, missing))}.")

    # ---- students ----
    warnings: list[str] = []
    students: list[ImportedStudent] = []
    class_i, sheet_i, version_i = col.get("class"), col.get("sheet id"), col.get("version")
    review_i = col.get("needs review")
    for r in body:
        name = r[0].strip()
        if name.upper() == "EXAM INFO" or name.upper().startswith("ANSWER KEY") or not name:
            continue
        where = f"{filename}, row for {name}"
        cells = {q: cell(r, i) for q, i in q_cols.items()}
        not_scanned = "not scanned" in cell(r, review_i).lower()
        answers, uncertain = None, set()
        if not (not_scanned and not any(cells.values())):
            answers = {}
            for q, value in cells.items():
                if value.endswith("?"):
                    uncertain.add(q)
                    value = value[:-1]
                answers[q] = _letters(value, f"{where}, Q{q}")
        exam_id, roster = _parse_sheet_id(cell(r, sheet_i))
        version: int | None = 1
        if num_versions > 1:
            v = cell(r, version_i).upper().removeprefix("V")
            version = int(v) if v.isdigit() and int(v) in keys else None
        students.append(ImportedStudent(
            name=name[:60], class_name=cell(r, class_i)[:60], exam_id=exam_id,
            roster_index=roster, version=version, answers=answers, uncertain=uncertain,
            written={n: _number(cell(r, i), f"{where}, Written {n}") for n, i in w_cols.items()},
            open_scores={q: _number(cell(r, i), f"{where}, Q{q} (open)")
                         for q, i in open_cols.items()},
        ))
    if not students:
        raise AnswerKeyError(f"{filename} has no student rows.")

    # ---- exam details (from EXAM INFO, or inferred for older files) ----
    used = [c for key in keys.values() for a in key.values() for c in a]
    used += [c for s in students if s.answers for a in s.answers.values() for c in a]
    try:
        num_choices = int(info["choices"])
    except (KeyError, ValueError):
        num_choices = max(MIN_CHOICES, max(used, default=0) + 1)
        warnings.append(f"{filename} doesn't record the number of answer choices; assuming "
                        f"{num_choices} (A–{CHOICE_LETTERS[num_choices - 1]}) from the answers.")
    if not MIN_CHOICES <= num_choices <= MAX_CHOICES or max(used, default=0) >= num_choices:
        raise AnswerKeyError(f"{filename}: the number of answer choices doesn't match the answers.")

    try:
        heights = tuple(float(h) for h in info["written"].split(";") if h.strip())
    except (KeyError, ValueError):
        heights = ()
    if len(heights) != len(w_cols):
        heights = tuple(2.0 for _ in w_cols)

    exam_ids = {s.exam_id for s in students if s.exam_id}
    exam_ids |= {i.strip().lower() for i in info.get("ids", "").split(";") if i.strip()}
    if not exam_ids:
        warnings.append(f"{filename} has no sheet IDs (it was made before they were added). "
                        "Late sheets will be matched to students by name.")

    exam_name = info.get("exam") or re.sub(r"_?results$", "", filename.rsplit(".", 1)[0]
                                           ).replace("_", " ").strip() or "Restored results"
    mode = info.get("scoring", "all")
    return ImportedResults(
        exam_name=exam_name[:60], num_questions=num_q, num_choices=num_choices,
        written_heights=heights, multi_mode=mode if mode in ("all", "any") else "all",
        keys=keys, num_versions=num_versions, exam_ids=exam_ids, students=students,
        open_questions=sorted(open_cols),
        warnings=warnings,
    )
