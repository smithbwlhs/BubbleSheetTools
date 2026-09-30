"""API tests through FastAPI's test client (auth bypassed; see conftest.py)."""

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import settings
from app.main import app
from app.sheet_layout import ExamSpec

from .synthetic import fill_bubbles, render_pdf_pages

client = TestClient(app)

SHEET = dict(class_name="Chem", exam_name="Quiz 1", num_questions=10, num_choices=4,
             written_heights=[1.5], names=["Ada Lovelace", "Alan Turing"])


def png(img):
    return cv2.imencode(".png", img)[1].tobytes()


def test_index_and_security_headers():
    r = client.get("/")
    assert r.status_code == 200 and "BubbleSheetTools" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_roster_parse():
    r = client.post("/api/roster/parse", json={"text": "A, B, C"})
    assert r.json() == {"names": ["A", "B", "C"]}
    r = client.post("/api/roster/parse", json={"text": "   "})
    assert r.status_code == 400 and "No student names" in r.json()["detail"]


@pytest.mark.parametrize("change,message", [
    ({"names": []}, "at least one student"),
    ({"class_name": ""}, "class name"),
    ({"exam_name": " "}, "exam name"),
    ({"num_questions": 0}, "between 1 and 100"),
    ({"num_choices": 12}, "between 2 and 10"),
    ({"written_heights": [30]}, "Written response boxes"),
])
def test_sheet_validation(change, message):
    r = client.post("/api/sheets", json={**SHEET, **change})
    assert r.status_code == 400 and message in r.json()["detail"]


def test_sheets_pdf():
    r = client.post("/api/sheets", json=SHEET)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert len(render_pdf_pages(r.content)) == 1 + 2  # key + 2 students (1 page each)


def test_grading_flow():
    pdf = client.post("/api/sheets", json=SHEET).content
    pages = render_pdf_pages(pdf)
    rng = np.random.default_rng(7)
    key = {q: {q % 4} for q in range(1, 11)}
    spec = ExamSpec("Chem", "Quiz 1", 10, 4, (1.5,))

    assert client.post("/api/grade/start").status_code == 200

    # Uploads are refused until there is a key.
    r = client.post("/api/grade/upload", files={"file": ("s.png", png(pages[1]), "image/png")})
    assert r.status_code == 400 and "answer key" in r.json()["detail"]
    assert client.post("/api/grade/done").status_code == 400

    r = client.post("/api/grade/key",
                    files={"file": ("key.png", png(fill_bubbles(pages[0], spec, key, rng)), "image/png")})
    assert r.status_code == 200, r.text
    assert r.json()["key"]["num_questions"] == 10

    ans1 = {q: set(v) for q, v in key.items()}
    ans2 = {q: {(q + 1) % 4} for q in range(1, 11)}
    ans2[1] = set()  # blank -> flagged
    for i, ans in enumerate([ans1, ans2]):
        img = fill_bubbles(pages[1 + i], spec, ans, rng)
        r = client.post("/api/grade/upload", files={"file": (f"s{i}.png", png(img), "image/png")})
        assert r.status_code == 200 and r.json()["summary"]["graded"]

    state = client.post("/api/grade/done").json()
    scores = {s["name"]: s["score"] for s in state["students"]}
    assert scores == {"Ada Lovelace": 10, "Alan Turing": 0}
    flag = next(f for f in state["flags"] if f["student"] == "Alan Turing")
    assert flag["question"] == 1 and flag["reason"] == "blank"

    snippet = client.get(f"/api/grade/snippet/{flag['student_index']}/1.png")
    assert snippet.headers["content-type"] == "image/png"

    # Teacher says question 1 was actually "B" (the correct answer).
    state = client.post("/api/grade/resolve",
                        json={"student_index": 2, "question": 1, "answer": "B"}).json()
    assert {s["name"]: s["score"] for s in state["students"]}["Alan Turing"] == 1

    assert client.post("/api/grade/written",
                       json={"student_index": 1, "number": 1, "score": 4}).status_code == 200

    csv = client.get("/api/grade/export/results.csv").content.decode("utf-8-sig")
    ada = next(line for line in csv.splitlines() if line.startswith("Ada Lovelace,Chem,"))
    assert ",10,10,100.0,4.0,14.0," in ada
    for name in ("item_analysis.csv", "score_distribution.png", "most_missed.png",
                 "choice_distribution.png", "all_results.zip"):
        r = client.get(f"/api/grade/export/{name}")
        assert r.status_code == 200 and r.content, name
        assert r.headers["cache-control"] == "no-store"

    assert client.post("/api/grade/clear").status_code == 200
    assert client.get("/api/grade/state").json() == {"active": False}
    assert client.get("/api/grade/export/results.csv").status_code == 409


def test_grading_requires_login(monkeypatch):
    monkeypatch.setattr(auth, "settings", type(settings)(auth_dev_bypass=False, supabase_url=""))
    r = client.post("/api/grade/start")
    assert r.status_code == 401
    # Creating sheets stays open to everyone.
    assert client.post("/api/sheets", json=SHEET).status_code == 200


def test_signup_requires_adult_confirmation():
    r = client.post("/api/auth/signup", json={"first_name": "A", "last_name": "B",
                                              "email": "a@b.co", "password": "longenough",
                                              "is_adult": False})
    assert r.status_code == 400 and "18 or older" in r.json()["detail"]


def test_change_password_needs_login_cookie():
    r = client.post("/api/auth/password", json={"password": "longenough"})
    assert r.status_code == 401 and "sign in again" in r.json()["detail"]


def test_import_results_csv():
    old = ("Student,Class,Sheet ID,MC correct,Needs review,Notes,Q1,Q2\n"
           "EXAM INFO,exam=Quiz 1,questions=2,choices=4,written=,scoring=all\n"
           "ANSWER KEY,,,2,,,A,C\n"
           "Ada Lovelace,Chem,0a1b2c3d-01,2,,,A,C\n")
    client.post("/api/grade/start")
    r = client.post("/api/grade/import", files={"file": ("results.csv", old.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    state = r.json()["state"]
    assert state["done"] and state["students"][0]["score"] == 2
    assert state["imported_files"] == ["results.csv"]
    r = client.post("/api/grade/import", files={"file": ("scan.pdf", b"%PDF-", "application/pdf")})
    assert r.status_code == 400 and "not a CSV" in r.json()["detail"]
    assert client.get("/api/grade/export/choice_distribution.png").status_code == 200
    client.post("/api/grade/clear")
