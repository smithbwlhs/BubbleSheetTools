"""Tests for the single upload path: any mix of answer keys, student sheets and CSVs."""

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.answer_key import parse_form_keys
from app.export import results_csv
from app.grading import GradingSession
from app.main import app
from app.sheet_generator import generate_sheets_pdf
from app.sheet_layout import ExamSpec

from .synthetic import distort, fill_bubbles, render_pdf_pages
from .test_pipeline import images_to_pdf

NAMES = ["Ada", "Alan", "Grace"]
KEY_TEXT = ["1. B\n2. AC\n3. FR\n4. D\n5. A", "1. C\n2. B\n3. FR\n4. A\n5. D"]


def as_sets(key) -> dict:
    return {q: set(v) for q, v in key.answers.items()}


@pytest.fixture(scope="module")
def exam():
    """Two-version exam whose PDF has pre-filled key sheets (pages 0-1)."""
    keys = parse_form_keys(KEY_TEXT)
    spec = ExamSpec("Alg 2", "Unit 3", 5, 4, num_versions=2, open_questions=(3,))
    pdf = generate_sheets_pdf(spec, NAMES, keys)
    pages = render_pdf_pages(pdf)
    rng = np.random.default_rng(21)
    # Ada and Grace took version 1 (perfect), Alan version 2 (perfect).
    students = [
        distort(fill_bubbles(pages[2], spec, as_sets(keys[1]), rng, version=1), rng, angle=2),
        distort(fill_bubbles(pages[3], spec, as_sets(keys[2]), rng, version=2), rng, angle=-2),
        distort(fill_bubbles(pages[4], spec, as_sets(keys[1]), rng, version=1), rng,
                upside_down=True),
    ]
    return spec, keys, pages, students


def scores(session) -> dict:
    return {s.name: (s.version, session.score(s)) for s in session.students.values()}


@pytest.mark.parametrize("keys_first", [True, False])
def test_keys_and_students_in_one_pdf(exam, keys_first):
    spec, keys, pages, students = exam
    key_pages = [pages[0], pages[1]]
    combined = images_to_pdf(key_pages + students if keys_first else students + key_pages)

    session = GradingSession(user_id="t")
    summary = session.process_file("scan.pdf", combined, 50)
    assert summary["keys"] == [1, 2] and sorted(summary["graded"]) == sorted(NAMES)
    assert scores(session) == {"Ada": (1, 4), "Alan": (2, 4), "Grace": (1, 4)}
    assert not [m for m in summary["messages"] if m["level"] == "error"]


def test_student_sheets_before_any_key_are_reported(exam):
    _, _, _, students = exam
    session = GradingSession(user_id="t")
    session.set_num_versions(2)
    summary = session.process_file("students.pdf", images_to_pdf(students), 50)
    assert summary["graded"] == [] and not session.students
    assert "not graded" in summary["messages"][0]["message"]
    assert "version 1, 2" in summary["messages"][0]["message"]


def test_key_is_for_one_version_in_a_combined_pdf(exam):
    _, _, pages, students = exam
    session = GradingSession(user_id="t")
    summary = session.process_file("scan.pdf", images_to_pdf([pages[0], pages[1]] + students),
                                   50, version=2)
    assert summary["keys"] == [2] and session.missing_versions() == [1]
    assert summary["graded"] == []  # version 1's key is still missing


def test_repeated_and_changed_key_sheets(exam):
    spec, keys, pages, students = exam
    session = GradingSession(user_id="t")
    session.process_file("first.pdf", images_to_pdf([pages[0], pages[1], students[0]]), 50)
    assert set(session.keys) == {1, 2} and len(session.students) == 1

    # The same key sheets again with more students: keys skipped, students graded.
    summary = session.process_file("second.pdf",
                                   images_to_pdf([pages[0], pages[1]] + students[1:]), 50)
    assert summary["keys"] == [] and len(summary["graded"]) == 2
    assert [m["level"] for m in summary["messages"]] == ["info", "info"]

    # A different version 1 key after grading started is refused (not silently applied).
    rng = np.random.default_rng(5)
    changed = fill_bubbles(render_pdf_pages(generate_sheets_pdf(spec, ["X"]))[0], spec,
                           {1: {0}, 2: {0}, 4: {0}, 5: {0}}, rng)
    summary = session.process_file("new_key.png", cv2.imencode(".png", changed)[1].tobytes(), 50)
    assert summary["keys"] == []
    assert any("different print batch" in m["message"] or "already been graded" in m["message"]
               for m in summary["messages"])
    assert as_sets(session.keys[1]) == as_sets(keys[1])


def test_csv_files_are_recognised(exam):
    spec, keys, pages, students = exam
    graded = GradingSession(user_id="t")
    graded.process_file("scan.pdf", images_to_pdf([pages[0], pages[1]] + students), 50)

    restored = GradingSession(user_id="t")
    summary = restored.process_file("results.csv", results_csv(graded), 50)
    assert summary["kind"] == "results" and summary["imported"]["added"] == 3

    key_session = GradingSession(user_id="t")
    summary = key_session.process_file("key.csv", b"Question,Version 1,Version 2\n1,A,B\n2,C,D\n",
                                       50)
    assert summary["kind"] == "key" and summary["keys"] == [1, 2]


def test_api_files_endpoint(exam):
    _, _, pages, students = exam
    client = TestClient(app)
    client.post("/api/grade/start")
    r = client.post("/api/grade/files",
                    files={"file": ("scan.pdf", images_to_pdf([pages[0], pages[1]] + students))})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"]["keys"] == [1, 2] and len(body["summary"]["graded"]) == 3
    assert body["state"]["ready"] and len(body["state"]["students"]) == 3
    r = client.post("/api/grade/files", files={"file": ("key.csv", b"1,A\n2,B\n")})
    assert r.status_code == 400  # a changed key after grading started
    client.post("/api/grade/clear")
