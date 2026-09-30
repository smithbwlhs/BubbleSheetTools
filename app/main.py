"""
BubbleSheetTools web server (FastAPI).

Routes
  Public (no account needed)
    GET  /                      the web app (static/index.html)
    POST /api/roster/parse      turn pasted text / CSV into a list of names
    POST /api/sheets            download the bubble sheet PDF

  Accounts (Supabase, see auth.py)
    POST /api/auth/signup | login | logout | forgot | reset | password
    GET  /api/auth/me

  Grading (signed-in teachers only; everything held in memory, see sessions.py)
    POST /api/grade/start       begin a new grading session
    GET  /api/grade/state       everything the grading screen shows
    POST /api/grade/key         upload the answer key (CSV, or scanned key sheet)
    POST /api/grade/upload      upload one file of student sheets
    POST /api/grade/done        teacher is finished uploading
    POST /api/grade/resolve     teacher's answer for a flagged question
    POST /api/grade/written     score for a hand-graded written response
    POST /api/grade/settings    scoring mode for multi-answer questions
    GET  /api/grade/snippet/{student}/{question}.png   image of a flagged row
    GET  /api/grade/export/{name}                      CSV / PNG / ZIP downloads
    POST /api/grade/clear       delete this session's data now
"""

import logging
import re
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, export, sessions
from .answer_key import AnswerKeyError
from .config import settings
from .grading import GradingSession, letters
from .roster import RosterError, parse_csv, parse_pasted
from .sheet_generator import generate_sheets_pdf
from .sheet_layout import ExamSpec, LayoutError, max_written_height_in

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bubblesheettools")

app = FastAPI(title="BubbleSheetTools", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Strict browser security headers; the app loads nothing from other sites."""
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; "
        "script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"  # never cache student data
    return response


def _bad(message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


def _safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:60] or "exam"


def _read_upload(file: UploadFile) -> bytes:
    """Read an uploaded file, enforcing the size limit."""
    limit = settings.max_upload_mb * 1024 * 1024
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise _bad(f"{file.filename} is larger than {settings.max_upload_mb} MB.", 413)
    if not data:
        raise _bad(f"{file.filename or 'The file'} is empty.")
    return data


# ================================================================ public ====

class RosterIn(BaseModel):
    text: str = Field("", max_length=200_000)
    source: str = "paste"  # "paste" or "csv"


@app.post("/api/roster/parse")
def roster_parse(body: RosterIn):
    try:
        names = parse_csv(body.text) if body.source == "csv" else parse_pasted(body.text)
    except RosterError as exc:
        raise _bad(str(exc))
    return {"names": names}


class SheetsIn(BaseModel):
    class_name: str = Field("", max_length=200)
    exam_name: str = Field("", max_length=200)
    num_questions: int = 0
    num_choices: int = 4
    written_heights: list[float] = Field(default_factory=list, max_length=50)
    names: list[str] = Field(default_factory=list, max_length=1000)


@app.get("/api/sheets/limits")
def sheet_limits():
    """Limits the form needs to validate input before submitting."""
    from .sheet_layout import MAX_CHOICES, MAX_QUESTIONS, MAX_WRITTEN, MIN_WRITTEN_HEIGHT_IN
    return {"max_questions": MAX_QUESTIONS, "max_choices": MAX_CHOICES,
            "max_written": MAX_WRITTEN, "min_written_height": MIN_WRITTEN_HEIGHT_IN,
            "max_written_height": max_written_height_in()}


@app.post("/api/sheets")
def make_sheets(body: SheetsIn):
    names = [n.strip() for n in body.names if n.strip()]
    if not names:
        raise _bad("Add at least one student name before creating sheets.")
    try:
        names = parse_pasted("\n".join(names))  # same cleaning/limits as the roster step
        spec = ExamSpec(class_name=body.class_name.strip(), exam_name=body.exam_name.strip(),
                        num_questions=body.num_questions, num_choices=body.num_choices,
                        written_heights=tuple(round(h, 2) for h in body.written_heights))
        pdf = generate_sheets_pdf(spec, names)
    except (LayoutError, RosterError, ValueError) as exc:
        raise _bad(str(exc))
    filename = f"{_safe_filename(spec.class_name)}_{_safe_filename(spec.exam_name)}_sheets.pdf"
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ============================================================== accounts ====

class SignupIn(BaseModel):
    first_name: str = Field("", max_length=100)
    last_name: str = Field("", max_length=100)
    email: str = Field("", max_length=254)
    password: str = Field("", max_length=200)
    is_adult: bool = False


class LoginIn(BaseModel):
    email: str = Field("", max_length=254)
    password: str = Field("", max_length=200)


class ForgotIn(BaseModel):
    email: str = Field("", max_length=254)


class ResetIn(BaseModel):
    access_token: str = Field("", max_length=4000)
    password: str = Field("", max_length=200)


def _site_url(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/"


def _auth_call(fn, *args):
    try:
        return fn(*args)
    except auth.AuthError as exc:
        raise _bad(str(exc), exc.status)


@app.post("/api/auth/signup")
def signup(body: SignupIn, request: Request):
    _auth_call(auth.sign_up, body.first_name, body.last_name, body.email, body.password,
               body.is_adult, _site_url(request))
    return {"message": "Account created. Check your email for a link to confirm your "
                       "address, then sign in."}


@app.post("/api/auth/login")
def login(body: LoginIn, response: Response):
    return {"user": _auth_call(auth.log_in, body.email, body.password, response)}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    user = None
    try:
        user = auth.current_user(request, response)
    except auth.AuthError:
        pass
    if user:
        sessions.clear(user["id"])  # signing out also discards grading data
    auth.log_out(request, response)
    return {"ok": True}


@app.post("/api/auth/forgot")
def forgot(body: ForgotIn, request: Request):
    _auth_call(auth.send_password_reset, body.email, _site_url(request))
    return {"message": "If that email has an account, a reset link is on its way."}


@app.post("/api/auth/reset")
def reset(body: ResetIn):
    _auth_call(auth.update_password, body.access_token, body.password)
    return {"message": "Password updated. You can sign in now."}


class PasswordIn(BaseModel):
    password: str = Field("", max_length=200)


@app.post("/api/auth/password")
def change_password(body: PasswordIn, request: Request, user: dict = Depends(auth.require_user)):
    """Signed-in teacher changes their password (uses their login cookie)."""
    token = request.cookies.get(auth.ACCESS_COOKIE)
    if not token:
        raise _bad("Please sign in again to change your password.", 401)
    _auth_call(auth.update_password, token, body.password)
    return {"message": "Your password has been changed."}


@app.get("/api/auth/me")
def me(request: Request, response: Response):
    try:
        user = auth.current_user(request, response)
    except auth.AuthError:
        user = None
    return {"user": user, "configured": bool(settings.supabase_url) or settings.auth_dev_bypass}


# =============================================================== grading ====

def _session(user: dict = Depends(auth.require_user)) -> GradingSession:
    session = sessions.get(user["id"])
    if session is None:
        raise _bad("Your grading session has ended (it expires after "
                   f"{settings.session_ttl_minutes} minutes of inactivity). Start a new one.", 409)
    session.touch()
    return session


def _state(session: GradingSession) -> dict:
    """Everything the grading screen needs, as JSON."""
    spec, key = session.spec, session.key
    students, flags = [], []
    total_q = len(key.answers) if key else 0
    for idx, s in sorted(session.students.items()):
        score = session.score(s) if (key and s.answers is not None) else None
        students.append({
            "index": idx, "name": s.name, "score": score, "possible": total_q,
            "percent": round(100 * score / total_q, 1) if score is not None and total_q else None,
            "bubble_page_read": s.answers is not None, "pages_seen": sorted(s.pages_seen),
            "open_flags": len(s.open_flags), "rescanned": s.rescanned,
            "written": [{"number": n, "score": v} for n, v in sorted(s.written_scores.items())],
        })
        for q, f in sorted(s.flags.items()):
            flags.append({
                "student_index": idx, "student": s.name, "question": q, "reason": f.reason,
                "description": f.describe(), "detected": letters(f.detected),
                "answer": letters(s.answers.get(q, frozenset())), "resolved": f.resolved,
                "multi_correct": len(key.answers.get(q, ())) > 1,
            })
    return {
        "key": None if key is None else {
            "source": session.key_source, "num_questions": key.num_questions,
            "multi_answer_questions": [q for q, a in key.answers.items() if len(a) > 1],
        },
        "exam": None if spec is None else {
            "class_name": spec.class_name, "exam_name": spec.exam_name,
            "num_questions": spec.num_questions, "num_choices": spec.num_choices,
            "num_written": len(spec.written_heights),
        },
        "students": students,
        "flags": flags,
        "messages": [m.__dict__ for m in session.messages],
        "multi_mode": session.multi_mode,
        "done": session.done,
        "expires_minutes": settings.session_ttl_minutes,
    }


@app.post("/api/grade/start")
def grade_start(user: dict = Depends(auth.require_user)):
    return _state(sessions.start(user["id"]))


@app.get("/api/grade/state")
def grade_state(user: dict = Depends(auth.require_user)):
    session = sessions.get(user["id"])
    return {"active": False} if session is None else {"active": True, **_state(session)}


@app.post("/api/grade/key")
def grade_key(file: UploadFile = File(...), session: GradingSession = Depends(_session)):
    try:
        session.set_key_from_upload(file.filename or "key", _read_upload(file))
    except AnswerKeyError as exc:
        raise _bad(str(exc))
    return _state(session)


@app.post("/api/grade/upload")
def grade_upload(file: UploadFile = File(...), session: GradingSession = Depends(_session)):
    if session.key is None:
        raise _bad("Upload the answer key before uploading student sheets.")
    data = _read_upload(file)
    try:
        summary = session.process_upload(file.filename or "upload", data,
                                         settings.max_pages_per_upload)
    except AnswerKeyError as exc:
        raise _bad(str(exc))
    session.done = False
    return {"summary": summary, "state": _state(session)}


@app.post("/api/grade/done")
def grade_done(session: GradingSession = Depends(_session)):
    if session.key is None:
        raise _bad("Upload the answer key first.")
    if not any(s.answers is not None for s in session.students.values()):
        raise _bad("No student sheets have been graded yet. Upload at least one sheet.")
    session.done = True
    return _state(session)


class ResolveIn(BaseModel):
    student_index: int
    question: int
    answer: str = Field("", max_length=20)  # letters, e.g. "B" or "AC"; "" = blank


@app.post("/api/grade/resolve")
def grade_resolve(body: ResolveIn, session: GradingSession = Depends(_session)):
    try:
        session.resolve(body.student_index, body.question, body.answer)
    except ValueError as exc:
        raise _bad(str(exc))
    return _state(session)


class WrittenIn(BaseModel):
    student_index: int
    number: int
    score: float | None = None


@app.post("/api/grade/written")
def grade_written(body: WrittenIn, session: GradingSession = Depends(_session)):
    try:
        session.set_written_score(body.student_index, body.number, body.score)
    except ValueError as exc:
        raise _bad(str(exc))
    return {"ok": True}


class SettingsIn(BaseModel):
    multi_mode: str


@app.post("/api/grade/settings")
def grade_settings(body: SettingsIn, session: GradingSession = Depends(_session)):
    try:
        session.set_multi_mode(body.multi_mode)
    except ValueError as exc:
        raise _bad(str(exc))
    return _state(session)


@app.get("/api/grade/snippet/{student_index}/{question}.png")
def grade_snippet(student_index: int, question: int,
                  session: GradingSession = Depends(_session)):
    student = session.students.get(student_index)
    flag = student.flags.get(question) if student else None
    if flag is None or not flag.snippet_png:
        raise _bad("Image not found.", 404)
    return Response(flag.snippet_png, media_type="image/png")


_EXPORTS = {
    "results.csv": ("text/csv", export.results_csv),
    "item_analysis.csv": ("text/csv", export.item_analysis_csv),
    "score_distribution.png": ("image/png", export.score_distribution_png),
    "most_missed.png": ("image/png", export.most_missed_png),
    "choice_distribution.png": ("image/png", export.choice_distribution_png),
    "all_results.zip": ("application/zip", export.all_exports_zip),
}


@app.get("/api/grade/export/{name}")
def grade_export(name: str, inline: bool = False, session: GradingSession = Depends(_session)):
    if name not in _EXPORTS:
        raise _bad("Unknown export.", 404)
    if session.key is None or not any(s.answers is not None for s in session.students.values()):
        raise _bad("There are no graded sheets to export yet.")
    media_type, build = _EXPORTS[name]
    with session.lock:
        data = build(session)
    prefix = f"{_safe_filename(session.spec.class_name)}_{_safe_filename(session.spec.exam_name)}"
    disposition = "inline" if inline else "attachment"
    return Response(data, media_type=media_type,
                    headers={"Content-Disposition": f'{disposition}; filename="{prefix}_{name}"'})


@app.post("/api/grade/clear")
def grade_clear(user: dict = Depends(auth.require_user)):
    sessions.clear(user["id"])
    return {"ok": True}


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    """Log unexpected errors without request bodies (which may hold student data)."""
    log.exception("Unexpected error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Something went wrong on the server. Please try again."},
                        status_code=500)


# ================================================================ static ====

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
