"""Tests for re-uploading results CSVs (restore, combine periods, late scans)."""

import cv2
import numpy as np
import pytest

from app.answer_key import AnswerKey, AnswerKeyError
from app.export import results_csv
from app.grading import Flag, GradingSession
from app.sheet_generator import generate_sheets_pdf
from app.sheet_layout import ExamSpec

from .synthetic import fill_bubbles, render_pdf_pages

KEY = {1: frozenset({0}), 2: frozenset({1}), 3: frozenset({0, 2}), 4: frozenset({3})}


def graded_session(spec: ExamSpec, students: list[tuple[str, dict, float | None]]) -> GradingSession:
    """A session as if these students' sheets had been scanned."""
    session = GradingSession(user_id="t")
    session.keys = {1: AnswerKey(answers=dict(KEY), num_choices=spec.num_choices, exam_id=spec.exam_id)}
    session.spec = spec
    session.exam_ids = {spec.exam_id}
    for i, (name, answers, written) in enumerate(students, start=1):
        s = session._add_student(name=name, class_name=spec.class_name, exam_id=spec.exam_id,
                                 roster_index=i, written_scores={1: written})
        s.answers = {q: frozenset(v) for q, v in answers.items()}
        s.pages_seen = {1}
    session.done = True
    return session


def spec_for(class_name: str) -> ExamSpec:
    return ExamSpec(class_name, "Unit 4 Test", 4, 5, (2.5,))


PERFECT = {1: {0}, 2: {1}, 3: {0, 2}, 4: {3}}
HALF = {1: {0}, 2: {1}, 3: {0}, 4: {2}}


def test_round_trip_restores_everything():
    original = graded_session(spec_for("Bio P3"), [("Ada", PERFECT, 4.0), ("Alan", HALF, None)])
    alan = original.students[2]
    alan.flags[4] = Flag(4, "unclear", frozenset({2}), b"png")  # unchecked flag -> "D?" in CSV
    original.multi_mode = "any"
    data = results_csv(original)

    restored = GradingSession(user_id="t")
    summary = restored.import_results("results.csv", data)
    assert summary == {"file": "results.csv", "added": 2, "replaced": 0, "warnings": []}
    assert restored.done and restored.multi_mode == "any"
    assert restored.keys[1].answers == KEY
    assert restored.spec.num_choices == 5 and restored.spec.written_heights == (2.5,)
    assert restored.exam_ids == original.exam_ids

    ada, alan2 = restored.ordered_students()
    assert (ada.name, ada.class_name, ada.roster_index) == ("Ada", "Bio P3", 1)
    assert ada.written_scores == {1: 4.0} and restored.score(ada) == 4
    assert restored.score(alan2) == original.score(alan)
    assert list(alan2.flags) == [4] and alan2.flags[4].reason == "csv"
    assert results_csv(restored) == data  # nothing lost or changed


def test_edited_in_excel_still_loads():
    data = results_csv(graded_session(spec_for("Bio P3"), [("Ada", PERFECT, 4.0)]))
    text = data.decode("utf-8-sig").replace("\r\n", "\n")
    # Excel edits: a written score typed in, score columns now stale, saved as cp1252.
    text = text.replace(",4.0,", ",6.5,")
    restored = GradingSession(user_id="t")
    restored.import_results("results.csv", text.encode("cp1252"))
    assert restored.ordered_students()[0].written_scores == {1: 6.5}


def test_combining_class_periods():
    p3 = graded_session(spec_for("Bio P3"), [("Ada", PERFECT, 3.0), ("Alan", HALF, 2.0)])
    p5 = graded_session(spec_for("Bio P5"), [("Grace", HALF, 1.0)])
    session = GradingSession(user_id="t")
    session.import_results("p3.csv", results_csv(p3))
    summary = session.import_results("p5.csv", results_csv(p5))
    assert summary["added"] == 1 and summary["replaced"] == 0  # roster #1 in P5 is a different student
    assert session.class_names() == ["Bio P3", "Bio P5"]
    assert session.exam_ids == p3.exam_ids | p5.exam_ids
    assert [s.name for s in session.ordered_students()] == ["Ada", "Alan", "Grace"]

    combined = results_csv(session).decode("utf-8-sig")
    assert "Grace,Bio P5," in combined


def test_same_student_again_is_replaced_and_noted():
    first = graded_session(spec_for("Bio P3"), [("Ada", HALF, None), ("Alan", HALF, None)])
    retake = GradingSession(user_id="t")
    retake.import_results("first.csv", results_csv(first))
    first.students[1].answers = {q: frozenset(v) for q, v in PERFECT.items()}  # Ada's retake
    summary = retake.import_results("retake.csv", results_csv(first))
    assert summary["replaced"] == 2 and len(retake.students) == 2
    ada = retake.ordered_students()[0]
    assert retake.score(ada) == 4 and ada.notes == ["replaced by retake.csv"]


def test_different_answer_key_is_rejected():
    a = graded_session(spec_for("Bio P3"), [("Ada", PERFECT, None)])
    b = graded_session(spec_for("Bio P5"), [("Grace", PERFECT, None)])
    b.keys[1].answers[1] = frozenset({1})
    session = GradingSession(user_id="t")
    session.import_results("a.csv", results_csv(a))
    with pytest.raises(AnswerKeyError, match="different answer key"):
        session.import_results("b.csv", results_csv(b))


@pytest.mark.parametrize("text,message", [
    ("hello,world\n1,2\n", "doesn't look like a results CSV"),
    ("Student,Q1\nAda,A\n", "missing its ANSWER KEY"),
    ("Student,Q1\nANSWER KEY,\nAda,A\n", "no answer for Q1"),
    ("Student,Q1\nANSWER KEY,A\n", "no student rows"),
    ("Student,Q1\nANSWER KEY,A\nAda,Z\n", "not a valid answer"),
    ("Student,Q1,Q3\nANSWER KEY,A,B\nAda,A,B\n", "none missing"),
])
def test_bad_results_files(text, message):
    with pytest.raises(AnswerKeyError, match=message):
        GradingSession(user_id="t").import_results("r.csv", text.encode())


def test_older_results_file_without_new_columns():
    old = ("Student,MC correct,MC possible,MC percent,Needs review,Q1,Q2\n"
           "ANSWER KEY,2,2,100.0,,A,C\n"
           "Ada Lovelace,2,2,100.0,,A,C\n"
           "Alan Turing,,2,,Bubble page not scanned,,\n")
    session = GradingSession(user_id="t")
    summary = session.import_results("Bio_Unit_4_results.csv", old.encode())
    assert summary["added"] == 2
    assert any("answer choices" in w for w in summary["warnings"])
    assert session.spec.num_choices == 3 and session.spec.exam_name == "Bio Unit 4"
    ada, alan = session.ordered_students()
    assert session.score(ada) == 2 and alan.answers is None


def test_late_scans_after_restoring():
    """A student absent on test day is scanned later and added to the restored results."""
    spec = ExamSpec("Bio P3", "Unit 4 Test", 4, 5, (2.5,))
    pages = render_pdf_pages(generate_sheets_pdf(spec, ["Ada", "Alan", "Grace"]))
    first = graded_session(spec, [("Ada", PERFECT, 3.0), ("Alan", HALF, 2.0)])

    session = GradingSession(user_id="t")
    session.import_results("results.csv", results_csv(first))
    session.done = False

    rng = np.random.default_rng(1)
    grace = fill_bubbles(pages[3], spec, PERFECT, rng)  # pages: key, Ada, Alan, Grace
    summary = session.process_upload("grace.png", cv2.imencode(".png", grace)[1].tobytes(), 10)
    assert summary["graded"] == ["Grace"]
    late = next(s for s in session.students.values() if s.name == "Grace")
    assert late.notes == ["late scan"] and session.score(late) == 4 and late.roster_index == 3

    # Re-scanning Alan's paper replaces his restored row (same sheet ID).
    alan_page = fill_bubbles(pages[2], spec, PERFECT, rng)
    session.process_upload("alan.png", cv2.imencode(".png", alan_page)[1].tobytes(), 10)
    alan = next(s for s in session.students.values() if s.name == "Alan")
    assert len(session.students) == 3 and "rescanned" in alan.notes and session.score(alan) == 4

    # Sheets from another print batch are still refused.
    other = render_pdf_pages(generate_sheets_pdf(ExamSpec("Bio P3", "Unit 4 Test", 4, 5, (2.5,)),
                                                 ["Stranger"]))[1]
    summary = session.process_upload("x.png", cv2.imencode(".png", other)[1].tobytes(), 10)
    assert "different exam" in summary["messages"][0]["message"]
