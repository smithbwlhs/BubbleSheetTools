"""
Grading: read uploaded sheets, compare them to the key and keep the results
for one teacher's grading session.

Exams can have up to 4 versions (e.g. scrambled question or choice order).
Each version has its own answer key; students bubble their version on the
sheet and are graded against that version's key. Analysis is per version.

Everything here lives in memory only (see sessions.py). Student names come
from the QR codes on the sheets and are discarded when the session ends.
"""

import threading
import time
from dataclasses import dataclass, field

from .answer_key import AnswerKey, AnswerKeyError, parse_key_csv, parse_versioned_key_csv
from .results_import import ImportedResults, parse_results_csv
from .scanner.align import AlignError, locate_page
from .scanner.bubbles import QuestionRead, read_bubbles, row_snippet_png
from .scanner.loader import UploadError, load_pages
from .sheet_layout import (
    CHOICE_LETTERS, MAX_VERSIONS, ExamSpec, build_layout, decode_qr, format_question_list,
)

MULTI_MODES = ("all", "any")  # how questions with several correct answers are scored
VERSION_QUESTION = 0          # flags / reads keyed 0 are about the version row


def letters(choices) -> str:
    """{0, 2} -> "AC"."""
    return "".join(CHOICE_LETTERS[c] for c in sorted(choices))


def parse_letters(text: str, num_choices: int) -> frozenset[int]:
    """"ac" -> {0, 2}. Empty string means a blank answer."""
    out = set()
    for ch in (text or "").upper():
        if ch.isspace() or ch == ",":
            continue
        idx = CHOICE_LETTERS.find(ch)
        if idx < 0 or idx >= num_choices:
            raise ValueError(f"'{ch}' is not a valid choice for this exam.")
        out.add(idx)
    return frozenset(out)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")  # some Excel "CSV" saves


# ------------------------------------------------------------ records ----

@dataclass
class Flag:
    """A question (or the version row, question 0) that needs a human look."""

    question: int
    reason: str               # see describe()
    detected: frozenset[int]  # what the scanner thinks is marked (versions: index = v - 1)
    snippet_png: bytes        # cropped image of the row (empty when restored from a CSV)
    resolved: bool = False

    def describe(self) -> str:
        return {
            "unclear": "A bubble is faint or partly erased.",
            "multiple": "More than one bubble is filled in.",
            "blank": "No bubble is filled in.",
            "csv": "Still marked for checking in the uploaded results CSV "
                   "(no scan image is available; check the paper sheet).",
            "version-blank": "No version is bubbled in. Pick the student's version.",
            "version-multiple": "More than one version is bubbled in. Pick the student's version.",
            "version-unclear": "The version bubble is faint or partly erased. "
                               "Pick the student's version.",
            "version-csv": "The version was still unknown in the uploaded results CSV. "
                           "Check the paper sheet and pick the version.",
        }[self.reason]


@dataclass
class StudentResult:
    id: int          # unique within this grading session (what the web page refers to)
    name: str
    class_name: str = ""
    exam_id: str = ""                # print batch, from the QR code
    roster_index: int | None = None  # position in that batch's roster, if known
    version: int | None = 1          # exam version; None until known (multi-version exams)
    answers: dict[int, frozenset[int]] | None = None  # None until page 1 is scanned
    flags: dict[int, Flag] = field(default_factory=dict)
    written_scores: dict[int, float | None] = field(default_factory=dict)
    open_scores: dict[int, float | None] = field(default_factory=dict)  # by question number
    pages_seen: set[int] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)   # e.g. "rescanned", "late scan"

    @property
    def open_flags(self) -> list[Flag]:
        return [f for f in self.flags.values() if not f.resolved]

    @property
    def sheet_id(self) -> str:
        """Batch + roster number, e.g. "3f9c01ab-07" (blank if unknown)."""
        if self.exam_id and self.roster_index is not None:
            return f"{self.exam_id}-{self.roster_index:02d}"
        return ""

    def add_note(self, note: str) -> None:
        if note not in self.notes:
            self.notes.append(note)


@dataclass
class PageMessage:
    source: str   # e.g. "scan.pdf, page 3"
    message: str
    level: str = "error"  # "error" or "info"


@dataclass
class GradingSession:
    user_id: str
    created: float = field(default_factory=time.time)
    touched: float = field(default_factory=time.time)
    # One answer key per exam version (just {1: key} for single-version exams).
    keys: dict[int, AnswerKey] = field(default_factory=dict)
    key_sources: dict[int, str] = field(default_factory=dict)
    num_versions: int = 1
    spec: ExamSpec | None = None
    # Print batches whose sheets belong to this session. Usually one; several
    # when results CSVs from different class periods are combined.
    exam_ids: set[str] = field(default_factory=set)
    students: dict[int, StudentResult] = field(default_factory=dict)
    messages: list[PageMessage] = field(default_factory=list)
    imported_files: list[str] = field(default_factory=list)
    multi_mode: str = "all"
    done: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _next_id: int = 1

    # ------------------------------------------------------------ keys ----

    @property
    def versions(self) -> list[int]:
        return list(range(1, self.num_versions + 1))

    def missing_versions(self) -> list[int]:
        return [v for v in self.versions if v not in self.keys]

    @property
    def ready(self) -> bool:
        """True when every version has a key (so student sheets can be graded)."""
        return bool(self.keys) and not self.missing_versions()

    @property
    def num_questions(self) -> int:
        """All numbered questions on the sheet, multiple choice and open."""
        if self.spec:
            return self.spec.num_questions
        return next(iter(self.keys.values())).num_questions if self.keys else 0

    @property
    def open_questions(self) -> list[int]:
        """Open-response questions: hand-scored, not part of the MC score."""
        if self.spec:
            return list(self.spec.open_questions)
        return sorted(next(iter(self.keys.values())).open_questions) if self.keys else []

    @property
    def questions(self) -> list[int]:
        """Multiple choice question numbers (the ones the scanner grades)."""
        open_set = set(self.open_questions)
        return [q for q in range(1, self.num_questions + 1) if q not in open_set]

    def key_for(self, version: int | None) -> AnswerKey | None:
        return self.keys.get(version) if version else None

    def set_num_versions(self, n: int) -> None:
        """Teacher says how many versions the exam has (for CSV keys)."""
        if not 1 <= n <= MAX_VERSIONS:
            raise ValueError(f"An exam can have 1 to {MAX_VERSIONS} versions.")
        with self.lock:
            self._no_students_yet()
            if self.spec and self.exam_ids and n != self.spec.num_versions:
                raise ValueError(f"The scanned key sheet says this exam has "
                                 f"{self.spec.num_versions} version(s).")
            self.num_versions = n
            for v in [v for v in self.keys if v > n]:
                del self.keys[v], self.key_sources[v]
            self.touch()

    def set_key_from_upload(self, filename: str, data: bytes, version: int | None = None) -> list[int]:
        """Load answer key(s) from a CSV or scanned key sheet(s).

        * Scanned key sheets say which version they are (QR code); several can
          be in one PDF.
        * A CSV with "Version 1, Version 2, ..." columns sets every version.
        * Any other CSV is the key for `version` (chosen on the page; default 1).
        Returns the versions that were set.
        """
        if not data:
            raise AnswerKeyError("The answer key file is empty.")
        spec, has_version_columns = None, False
        if filename.lower().endswith((".csv", ".txt")):
            text = _decode(data)
            found = parse_versioned_key_csv(text)
            has_version_columns = found is not None
            if found is None:
                found = {version or 1: parse_key_csv(text)}
        else:
            found, spec = _read_key_sheets(filename, data)

        with self.lock:
            self._no_students_yet()
            if spec is not None:
                if self.spec and self.exam_ids and spec.exam_id not in self.exam_ids:
                    raise AnswerKeyError(
                        "That key sheet is from a different print batch than the key(s) "
                        "already loaded. Start a new session to switch exams.")
                self.spec, self.num_versions = spec, spec.num_versions
                self.exam_ids = {spec.exam_id}
            elif has_version_columns:
                # "Version 1, Version 2, ..." columns say how many versions there are.
                self.num_versions = max(self.num_versions, max(found))
            elif max(found) > self.num_versions:
                raise AnswerKeyError(
                    f"This exam is set to {self.num_versions} version(s); there is no "
                    f"version {max(found)}. Change the number of versions first.")
            others = [k for v, k in self.keys.items() if v not in found]
            for key in found.values():
                if others and key.num_questions != others[0].num_questions:
                    raise AnswerKeyError(
                        f"This key has {key.num_questions} questions but the key already "
                        f"loaded has {others[0].num_questions}. All versions must match.")
                expected = (set(self.spec.open_questions) if self.spec and self.exam_ids
                            else set(others[0].open_questions) if others else None)
                if expected is not None and set(key.open_questions) != expected:
                    raise AnswerKeyError(
                        f"This key marks question(s) {_qlist(key.open_questions) or 'none'} as "
                        f"open, but the exam's open questions are {_qlist(expected) or 'none'}.")
            for v, key in found.items():
                self.keys[v], self.key_sources[v] = key, filename
            self.touch()
            return sorted(found)

    def _no_students_yet(self) -> None:
        if self.students:
            raise AnswerKeyError(
                "Sheets have already been graded with the current key(s). "
                "Start a new grading session to change the answer key.")

    # -------------------------------------------------------- students ----

    def ordered_students(self) -> list[StudentResult]:
        """Students grouped by class, then in roster (or name) order."""
        return sorted(self.students.values(), key=lambda s: (
            s.class_name.lower(), s.exam_id,
            s.roster_index if s.roster_index is not None else 10_000, s.name.lower()))

    def class_names(self) -> list[str]:
        names = sorted({s.class_name for s in self.students.values() if s.class_name})
        return names or ([self.spec.class_name] if self.spec and self.spec.class_name else [])

    def _add_student(self, **fields) -> StudentResult:
        student = StudentResult(id=self._next_id, **fields)
        self.students[student.id] = student
        self._next_id += 1
        return student

    def _find_student(self, exam_id: str, roster_index: int | None,
                      name: str) -> StudentResult | None:
        """Is this student already in the session? Match on sheet ID when both
        sides know it, otherwise on name (older CSVs have no sheet IDs)."""
        for s in self.students.values():
            if s.exam_id and exam_id and s.roster_index is not None and roster_index is not None:
                if s.exam_id == exam_id and s.roster_index == roster_index:
                    return s
            elif s.name.strip().lower() == name.strip().lower():
                return s
        return None

    # ------------------------------------------------- results imports ----

    def import_results(self, filename: str, data: bytes) -> dict:
        """Load (or merge in) a results CSV previously downloaded from this site."""
        if not data:
            raise AnswerKeyError(f"{filename} is empty.")
        imported = parse_results_csv(filename, _decode(data))

        with self.lock:
            self._merge_exam(imported, filename)
            added = replaced = 0
            for row in imported.students:
                existing = self._find_student(row.exam_id, row.roster_index, row.name)
                if existing is None:
                    student = self._add_student(name=row.name, class_name=row.class_name,
                                                exam_id=row.exam_id,
                                                roster_index=row.roster_index)
                    added += 1
                else:
                    student = existing
                    student.add_note(f"replaced by {filename}")
                    replaced += 1
                student.version = row.version
                student.answers = dict(row.answers) if row.answers is not None else None
                student.written_scores = dict(row.written)
                student.open_scores = dict(row.open_scores)
                student.pages_seen = {1} if row.answers is not None else set()
                student.flags = {
                    q: Flag(q, "csv", student.answers.get(q, frozenset()), b"")
                    for q in row.uncertain
                }
                if row.version is None and row.answers is not None:
                    student.flags[VERSION_QUESTION] = Flag(VERSION_QUESTION, "version-csv",
                                                           frozenset(), b"")
            self.imported_files.append(filename)
            self.done = True
            self.touch()
        return {"file": filename, "added": added, "replaced": replaced,
                "warnings": imported.warnings}

    def _merge_exam(self, imported: ImportedResults, filename: str) -> None:
        """Adopt the imported key(s)/exam, or check they match those loaded."""
        if not self.keys:
            self.keys = {v: AnswerKey(answers=dict(a), num_choices=imported.num_choices,
                                      open_questions=frozenset(imported.open_questions))
                         for v, a in imported.keys.items()}
            self.key_sources = {v: filename for v in imported.keys}
            self.num_versions = imported.num_versions
            first_class = next((s.class_name for s in imported.students if s.class_name), "")
            self.spec = ExamSpec(
                class_name=first_class, exam_name=imported.exam_name,
                num_questions=imported.num_questions, num_choices=imported.num_choices,
                written_heights=imported.written_heights,
                exam_id=min(imported.exam_ids, default="restored"),
                num_versions=imported.num_versions,
                open_questions=tuple(imported.open_questions))
            self.multi_mode = imported.multi_mode
        else:
            mine = {v: dict(k.answers) for v, k in self.keys.items()}
            if mine != {v: dict(a) for v, a in imported.keys.items()}:
                raise AnswerKeyError(
                    f"{filename} has a different answer key from the results already loaded, "
                    "so it can't be combined with them.")
            if set(self.open_questions) != set(imported.open_questions):
                raise AnswerKeyError(
                    f"{filename} has different open questions from the results already loaded.")
            if len(self.spec.written_heights) != len(imported.written_heights):
                raise AnswerKeyError(
                    f"{filename} has a different number of written responses from the "
                    "results already loaded.")
            if imported.num_choices > self.spec.num_choices:
                self.spec = ExamSpec(**{**self.spec.__dict__, "num_choices": imported.num_choices})
        self.exam_ids |= imported.exam_ids

    # --------------------------------------------------------- uploads ----

    def process_upload(self, filename: str, data: bytes, max_pages: int) -> dict:
        """Read every page of an uploaded file. Returns a short summary."""
        if not self.keys:
            raise AnswerKeyError("Upload the answer key before uploading student sheets.")
        if self.missing_versions():
            missing = ", ".join(map(str, self.missing_versions()))
            raise AnswerKeyError(f"Upload the answer key for version {missing} before "
                                 "uploading student sheets.")
        try:
            images = load_pages(filename, data, max_pages=max_pages)
        except UploadError as exc:
            msg = PageMessage(filename, str(exc))
            with self.lock:
                self.messages.append(msg)
            return {"file": filename, "pages": 0, "graded": [], "messages": [msg.__dict__]}

        graded, messages = [], []
        for page_no, image in enumerate(images, start=1):
            source = filename if len(images) == 1 else f"{filename}, page {page_no}"
            try:
                name = self._process_page(image, source)
                if name:
                    graded.append(name)
            except PageProblem as exc:
                messages.append(PageMessage(source, str(exc), exc.level))

        with self.lock:
            self.messages.extend(messages)
            self.touch()
        return {"file": filename, "pages": len(images), "graded": graded,
                "messages": [m.__dict__ for m in messages]}

    def _process_page(self, image, source: str) -> str | None:
        """Grade one page image. Returns the student's name if page 1 was read."""
        try:
            canon, qr_text = locate_page(image)
        except AlignError as exc:
            raise PageProblem(
                f"{exc} Make sure the whole sheet, including all four corner squares, "
                "is visible and in focus.") from None
        try:
            spec, who, page = decode_qr(qr_text)
        except ValueError as exc:
            raise PageProblem(str(exc)) from None

        if who.kind == "k":
            raise PageProblem("This is an answer key sheet; it was skipped.", level="info")

        with self.lock:
            self._check_same_exam(spec)
            student = self._find_student(spec.exam_id, who.student_index, who.student_name)
            if student is None:
                student = self._add_student(
                    name=who.student_name, class_name=spec.class_name, exam_id=spec.exam_id,
                    roster_index=who.student_index,
                    written_scores={n: None for n in range(1, len(spec.written_heights) + 1)},
                    open_scores={q: None for q in spec.open_questions})
                if self.imported_files:
                    student.add_note("late scan")
            elif not student.exam_id:
                # Matched by name to a row from an older CSV; remember the sheet ID now.
                student.exam_id, student.roster_index = spec.exam_id, who.student_index

            if page != 1:
                student.pages_seen.add(page)
                return None
            if student.answers is not None:
                student.add_note("rescanned")

        # Bubble reading is the slow part; do it outside the lock.
        layout = build_layout(spec)[0]
        reads = read_bubbles(canon, layout)
        flags = {}

        version_read = reads.pop(VERSION_QUESTION, None)
        version = 1
        if version_read is not None:
            version, reason = _read_version(version_read)
            if reason:
                flags[VERSION_QUESTION] = Flag(VERSION_QUESTION, reason, version_read.marked,
                                               row_snippet_png(canon, layout, VERSION_QUESTION))

        answers = {}
        for q, read in reads.items():
            answers[q] = read.marked
            reason = self._flag_reason(q, read, version)
            if reason:
                flags[q] = Flag(q, reason, read.marked, row_snippet_png(canon, layout, q))

        with self.lock:
            student.version, student.answers, student.flags = version, answers, flags
            student.pages_seen.add(1)
        return student.name

    def _check_same_exam(self, spec: ExamSpec) -> None:
        """Make sure an uploaded sheet belongs to the same exam as the key(s)."""
        if not self.exam_ids:
            # Keys came from CSVs (or an older results CSV without sheet IDs):
            # the first sheet defines the print batch, but it must match the
            # keys' number of questions, choices and versions.
            if self.num_questions != spec.num_questions:
                raise PageProblem(
                    f"The answer key has {self.num_questions} questions but this sheet has "
                    f"{spec.num_questions}. Check that the key matches this exam.")
            too_high = [q for k in self.keys.values() for q, ch in k.answers.items()
                        if max(ch) >= spec.num_choices]
            if too_high:
                raise PageProblem(
                    f"The answer key uses choices this sheet doesn't have (question {too_high[0]}).")
            key_open = set(next(iter(self.keys.values())).open_questions)
            if key_open != set(spec.open_questions):
                raise PageProblem(
                    f"The answer key's open questions ({_qlist(key_open) or 'none'}) don't "
                    f"match this sheet's ({_qlist(spec.open_questions) or 'none'}).")
            if spec.num_versions != self.num_versions:
                raise PageProblem(
                    f"This sheet is for an exam with {spec.num_versions} version(s), but "
                    f"{self.num_versions} answer key version(s) are set up.")
            if self.spec is None:
                self.spec = spec
            self.exam_ids.add(spec.exam_id)
        elif spec.exam_id not in self.exam_ids:
            raise PageProblem(
                f"This sheet is from a different exam ({spec.class_name} - {spec.exam_name}, "
                "different print batch) and was skipped.")

    def _flag_reason(self, q: int, read: QuestionRead, version: int | None) -> str | None:
        if read.unclear:
            return "unclear"
        if not read.marked:
            return "blank"
        # Several marks are only expected where the key has several answers.
        # With the version still unknown, flag if any version expects one answer.
        keys = [self.keys[version]] if version in self.keys else list(self.keys.values())
        if len(read.marked) > 1 and any(len(k.answers.get(q, ())) <= 1 for k in keys):
            return "multiple"
        return None

    # --------------------------------------------------------- editing ----

    def resolve(self, student_id: int, question: int, answer: str) -> None:
        """Teacher-confirmed answer for a flagged (or any) question.
        Question 0 is the version row; `answer` is then the version number."""
        with self.lock:
            student = self._student(student_id)
            if question == VERSION_QUESTION:
                if not answer.strip().isdigit() or int(answer) not in self.versions:
                    raise ValueError("Pick one of this exam's versions.")
                student.version = int(answer)
            else:
                if student.answers is None or question not in student.answers:
                    raise ValueError("That question was not found on this student's sheet.")
                student.answers[question] = parse_letters(answer, self.spec.num_choices)
            if question in student.flags:
                student.flags[question].resolved = True
            self.touch()

    def set_written_score(self, student_id: int, number: int, score: float | None) -> None:
        with self.lock:
            student = self._student(student_id)
            if number not in student.written_scores:
                raise ValueError("That written response does not exist on this exam.")
            if score is not None and not 0 <= score <= 1000:
                raise ValueError("Written scores must be between 0 and 1000.")
            student.written_scores[number] = score
            self.touch()

    def set_open_score(self, student_id: int, question: int, score: float | None) -> None:
        """Teacher's score for an open-response question."""
        with self.lock:
            student = self._student(student_id)
            if question not in self.open_questions:
                raise ValueError(f"Question {question} is not an open question on this exam.")
            if score is not None and not 0 <= score <= 1000:
                raise ValueError("Scores must be between 0 and 1000.")
            student.open_scores[question] = score
            self.touch()

    def set_multi_mode(self, mode: str) -> None:
        if mode not in MULTI_MODES:
            raise ValueError("Unknown scoring mode.")
        with self.lock:
            self.multi_mode = mode
            self.touch()

    def _student(self, student_id: int) -> StudentResult:
        student = self.students.get(student_id)
        if student is None:
            raise ValueError("Student not found in this grading session.")
        return student

    def touch(self) -> None:
        self.touched = time.time()

    # --------------------------------------------------------- scoring ----

    def is_correct(self, question: int, given: frozenset[int], version: int = 1) -> bool:
        correct = self.keys[version].answers.get(question, frozenset())
        if len(correct) > 1 and self.multi_mode == "any":
            return bool(given) and given <= correct
        return given == correct

    def score(self, student: StudentResult) -> int | None:
        """Number correct, or None if the sheet or the student's version is unknown."""
        if student.answers is None or student.version not in self.keys:
            return None
        return sum(self.is_correct(q, student.answers.get(q, frozenset()), student.version)
                   for q in self.questions)


class PageProblem(Exception):
    """A single page could not be graded; reported to the teacher."""

    def __init__(self, message: str, level: str = "error"):
        super().__init__(message)
        self.level = level


def _qlist(questions) -> str:
    return format_question_list(questions)


def _read_version(read: QuestionRead) -> tuple[int | None, str | None]:
    """Turn the version row into (version, flag reason)."""
    if read.unclear:
        return (min(read.marked) + 1 if len(read.marked) == 1 else None), "version-unclear"
    if not read.marked:
        return None, "version-blank"
    if len(read.marked) > 1:
        return None, "version-multiple"
    return min(read.marked) + 1, None


def _read_key_sheets(filename: str, data: bytes) -> tuple[dict[int, AnswerKey], ExamSpec]:
    """Read ANSWER KEY sheet(s) from a scanned PDF or photo. A PDF may hold
    the key sheets for several versions; each one's QR code says its version."""
    try:
        images = load_pages(filename, data, max_pages=20)
    except UploadError as exc:
        raise AnswerKeyError(str(exc)) from None

    keys: dict[int, AnswerKey] = {}
    spec_found = None
    problems = []
    for image in images:
        try:
            canon, qr_text = locate_page(image)
            spec, who, page = decode_qr(qr_text)
        except (AlignError, ValueError) as exc:
            problems.append(str(exc))
            continue
        if who.kind != "k":
            problems.append(f"That is {who.student_name}'s sheet, not an ANSWER KEY sheet.")
            continue
        if spec_found and spec.exam_id != spec_found.exam_id:
            raise AnswerKeyError(f"{filename} holds key sheets from different exams.")
        version = who.version or 1
        label = f"The version {version} answer key" if spec.num_versions > 1 else "The answer key"

        reads = read_bubbles(canon, build_layout(spec)[0])
        reads.pop(0, None)  # the version row is pre-printed on key sheets
        blank = [q for q, r in reads.items() if not r.marked and not r.unclear]
        unclear = [q for q, r in reads.items() if r.unclear]
        if blank or unclear:
            parts = []
            if blank:
                parts.append(f"no answer marked for question(s) {', '.join(map(str, blank[:15]))}")
            if unclear:
                parts.append(f"faint or partly erased marks on question(s) "
                             f"{', '.join(map(str, unclear[:15]))}")
            raise AnswerKeyError(
                f"{label} could not be read reliably: " + "; ".join(parts) +
                ". Darken or clean up those bubbles and re-scan, or upload a CSV key instead.")
        keys[version] = AnswerKey(answers={q: r.marked for q, r in reads.items()},
                                  num_choices=spec.num_choices, exam_id=spec.exam_id,
                                  open_questions=frozenset(spec.open_questions))
        spec_found = spec

    if keys:
        return keys, spec_found
    detail = problems[0] if problems else "No pages found."
    raise AnswerKeyError(f"No answer key sheet could be read from {filename}. {detail}")
