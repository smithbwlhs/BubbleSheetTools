"""Tests for open (not multiple choice) questions inside the numbering."""

import json

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.answer_key import AnswerKeyError, parse_key_csv
from app.export import item_analysis_csv, percent_scores, results_csv
from app.grading import GradingSession
from app.main import app
from app.sheet_generator import generate_sheets_pdf
from app.sheet_layout import (
    ExamSpec, LayoutError, build_layout, decode_qr, format_question_list, parse_question_list,
)

from .synthetic import fill_bubbles, render_pdf_pages

OPEN = (3, 7)
KEY = {q: {q % 4} for q in range(1, 11) if q not in OPEN}


def png(img) -> bytes:
    return cv2.imencode(".png", img)[1].tobytes()


# ------------------------------------------------------- number lists ----

@pytest.mark.parametrize("text,expected", [
    ("", ()), ("5", (5,)), ("5, 12-14,21", (5, 12, 13, 14, 21)), ("14-12; 3, 3", (3, 12, 13, 14)),
])
def test_parse_question_list(text, expected):
    assert parse_question_list(text, 30) == expected


@pytest.mark.parametrize("text", ["0", "5-31", "abc", "3-x"])
def test_bad_question_lists(text):
    with pytest.raises(LayoutError):
        parse_question_list(text, 30)


def test_format_question_list():
    assert format_question_list([21, 5, 12, 13, 14]) == "5,12-14,21"


# ------------------------------------------------------------- layout ----

def test_open_rows_have_a_box_instead_of_bubbles():
    spec = ExamSpec("Alg 2", "Unit 3", 10, 4, open_questions=OPEN)
    page = build_layout(spec)[0]
    assert {b.question for b in page.bubbles} == set(KEY)
    assert [box.question for box in page.open_boxes] == [3, 7]
    # The box covers exactly the span of a normal row's bubbles.
    row1 = sorted((b for b in page.bubbles if b.question == 1), key=lambda b: b.x)
    box = page.open_boxes[0]
    assert box.x == pytest.approx(row1[0].x - row1[0].r)
    assert box.x + box.w == pytest.approx(row1[-1].x + row1[-1].r)
    assert box.h == pytest.approx(2 * row1[0].r)
    # The question labels are still printed for open rows.
    assert {lbl.question for lbl in page.labels} == set(range(1, 11))


def test_qr_round_trip_and_old_codes():
    spec = ExamSpec("Alg 2", "Unit 3", 10, 4, open_questions=(3, 4, 5, 9))
    from app.sheet_layout import SheetIdentity, encode_qr
    text = encode_qr(spec, SheetIdentity("s", "Ada", 1), 1)
    assert json.loads(text)["o"] == "3-5,9"
    assert decode_qr(text)[0].open_questions == (3, 4, 5, 9)
    old = json.dumps({"v": 1, "x": "ab", "c": "B", "e": "U", "q": 5, "k": 4, "w": [],
                      "t": "s", "n": "A", "i": 1, "p": 1})
    assert decode_qr(old)[0].open_questions == ()


def test_every_question_open_is_rejected():
    with pytest.raises(LayoutError, match="At least one"):
        ExamSpec("B", "U", 3, 4, open_questions=(1, 2, 3)).validate()


# ------------------------------------------------------------ grading ----

@pytest.fixture(scope="module")
def graded():
    spec = ExamSpec("Alg 2", "Unit 3", 10, 4, (1.5,), open_questions=OPEN)
    pages = render_pdf_pages(generate_sheets_pdf(spec, ["Ada", "Alan"]))
    rng = np.random.default_rng(3)
    session = GradingSession(user_id="t")
    session.set_key_from_upload("key.png", png(fill_bubbles(pages[0], spec, KEY, rng)))
    wrong = {q: {(c + 1) % 4 for c in v} for q, v in KEY.items()}
    session.process_upload("ada.png", png(fill_bubbles(pages[1], spec, KEY, rng)), 10)
    session.process_upload("alan.png", png(fill_bubbles(pages[2], spec, wrong, rng)), 10)
    return session


def test_key_sheet_skips_open_questions(graded):
    key = graded.keys[1]
    assert set(key.answers) == set(KEY) and key.open_questions == frozenset(OPEN)
    assert graded.questions == sorted(KEY) and graded.open_questions == [3, 7]


def test_open_questions_not_machine_graded(graded):
    ada, alan = graded.ordered_students()
    assert graded.score(ada) == 8 and graded.score(alan) == 0  # out of 8, not 10
    assert not ada.flags and not alan.open_flags  # nothing to check on open rows
    assert ada.open_scores == {3: None, 7: None}


def test_open_scores_in_results_and_round_trip(graded):
    ada, alan = graded.ordered_students()
    graded.set_open_score(ada.id, 3, 4.0)
    graded.set_open_score(ada.id, 7, 2.5)
    graded.set_written_score(ada.id, 1, 1.0)
    with pytest.raises(ValueError, match="not an open question"):
        graded.set_open_score(ada.id, 4, 1.0)

    lines = results_csv(graded).decode("utf-8-sig").splitlines()
    header = lines[0].split(",")
    assert "Q3 (open)" in header and "Q7 (open)" in header
    assert "Total (MC + open + written)" in header
    key_row = next(line.split(",") for line in lines if line.startswith("ANSWER KEY"))
    assert key_row[header.index("Q3 (open)")] == "open"
    ada_row = next(line.split(",") for line in lines if line.startswith("Ada,"))
    assert ada_row[header.index("Q3 (open)")] == "4.0"
    assert ada_row[header.index("MC possible")] == "8"
    assert ada_row[header.index("Total (MC + open + written)")] == "15.5"  # 8 + 4 + 2.5 + 1

    # Chart percentages use the multiple choice questions only (Ada 8/8, Alan 0/8).
    assert percent_scores(graded) == [100.0, 0.0]

    items = item_analysis_csv(graded).decode("utf-8-sig").splitlines()
    assert [line.split(",")[0] for line in items[1:]] == [str(q) for q in sorted(KEY)]

    restored = GradingSession(user_id="t")
    restored.import_results("results.csv", results_csv(graded))
    assert restored.open_questions == [3, 7] and restored.questions == sorted(KEY)
    r_ada = next(s for s in restored.students.values() if s.name == "Ada")
    assert r_ada.open_scores == {3: 4.0, 7: 2.5} and restored.score(r_ada) == 8
    assert results_csv(restored) == results_csv(graded)


def test_csv_keys_and_open_questions():
    key = parse_key_csv("1,A\n2,open\n3,-\n4,B\n")
    assert set(key.answers) == {1, 4} and key.open_questions == {2, 3}
    with pytest.raises(AnswerKeyError, match="Write 'open'"):
        parse_key_csv("1,A\n3,B\n")

    spec = ExamSpec("Alg 2", "Unit 3", 10, 4, open_questions=OPEN)
    sheet = render_pdf_pages(generate_sheets_pdf(spec, ["Ada"]))[1]
    rows = "\n".join(f"{q},{'open' if q in OPEN else 'ABCD'[q % 4]}" for q in range(1, 11))
    good = GradingSession(user_id="t")
    good.set_key_from_upload("key.csv", rows.encode())
    assert good.process_upload("s.png", png(sheet), 10)["graded"] == ["Ada"]

    # A CSV key that treats question 3 as multiple choice doesn't match the sheet.
    bad_rows = rows.replace("3,open", "3,A")
    bad = GradingSession(user_id="t")
    bad.set_key_from_upload("key.csv", bad_rows.encode())
    msg = bad.process_upload("s.png", png(sheet), 10)["messages"][0]["message"]
    assert "open questions" in msg


# ---------------------------------------------------------------- API ----

client = TestClient(app)
SHEET = dict(class_name="Alg 2", exam_name="Unit 3", num_questions=10, num_choices=4,
             names=["Ada"])


def test_api_open_questions_field():
    assert client.post("/api/sheets", json={**SHEET, "open_questions": "3, 7"}).status_code == 200
    r = client.post("/api/sheets", json={**SHEET, "open_questions": "11"})
    assert r.status_code == 400 and "between 1 and 10" in r.json()["detail"]
    r = client.post("/api/sheets", json={**SHEET, "open_questions": "1-10"})
    assert r.status_code == 400 and "At least one" in r.json()["detail"]
