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
    POST /api/grade/import      continue from (or combine) results CSVs downloaded earlier
    POST /api/grade/upload      upload one file of student sheets
    POST /api/grade/done        teacher is finished uploading
    POST /api/grade/resolve     teacher's answer for a flagged question
    POST /api/grade/written     score for a hand-graded written response or open question
    POST /api/grade/settings    scoring mode for multi-answer questions
    GET  /api/grade/snippet/{student}/{question}.png   image of a flagged row
    GET  /api/grade/export/{name}                      CSV / PNG / ZIP downloads
    POST /api/grade/clear       delete this session's data now
"""

import logging
import re
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, export, sessions
from .answer_key import AnswerKeyError
from .config import settings
from .grading import GradingSession, letters
from .roster import RosterError, parse_csv, parse_pasted
from .sheet_generator import generate_sheets_pdf
from .sheet_layout import ExamSpec, LayoutError, max_written_height_in, parse_question_list

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
    num_versions: int = 1
    open_questions: str = Field("", max_length=400)  # e.g. "5, 12-14"
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
        open_qs = parse_question_list(body.open_questions, max(body.num_questions, 1))
        spec = ExamSpec(class_name=body.class_name.strip(), exam_name=body.exam_name.strip(),
                        num_questions=body.num_questions, num_choices=body.num_choices,
                        written_heights=tuple(round(h, 2) for h in body.written_heights),
                        num_versions=body.num_versions, open_questions=open_qs)
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
    spec = session.spec
    students, flags = [], []
    total_q = len(session.questions)  # multiple choice questions only
    for s in session.ordered_students():
        score = session.score(s)
        students.append({
            "index": s.id, "name": s.name, "class_name": s.class_name, "sheet_id": s.sheet_id,
            "version": s.version, "score": score, "possible": total_q,
            "percent": round(100 * score / total_q, 1) if score is not None and total_q else None,
            "bubble_page_read": s.answers is not None, "pages_seen": sorted(s.pages_seen),
            "open_flags": len(s.open_flags), "notes": s.notes,
            "written": [{"number": n, "score": v} for n, v in sorted(s.written_scores.items())],
            "open": [{"question": q, "score": s.open_scores.get(q)}
                     for q in session.open_questions],
        })
        for q, f in sorted(s.flags.items()):
            if q == 0:  # the version row: detected / answer are version numbers
                detected = "".join(str(c + 1) for c in sorted(f.detected))
                answer = str(s.version) if s.version else ""
                multi = False
            else:
                detected, answer = letters(f.detected), letters(s.answers.get(q, frozenset()))
                key = session.key_for(s.version)
                keys = [key] if key else list(session.keys.values())
                multi = all(len(k.answers.get(q, ())) > 1 for k in keys)
            flags.append({
                "student_index": s.id, "student": s.name, "question": q, "reason": f.reason,
                "description": f.describe(), "detected": detected, "answer": answer,
                "resolved": f.resolved, "multi_correct": multi, "has_image": bool(f.snippet_png),
            })
    return {
        "num_versions": session.num_versions,
        "keys": [{"version": v, "source": session.key_sources.get(v),
                  "loaded": v in session.keys} for v in session.versions],
        "ready": session.ready,
        "open_questions": session.open_questions,
        "key": None if not session.keys else {
            "num_questions": session.num_questions, "num_mc": total_q,
            "multi_answer_questions": sorted({q for k in session.keys.values()
                                              for q, a in k.answers.items() if len(a) > 1}),
        },
        "exam": None if spec is None else {
            "class_name": " / ".join(session.class_names()), "exam_name": spec.exam_name,
            "num_questions": spec.num_questions, "num_choices": spec.num_choices,
            "num_written": len(spec.written_heights),
        },
        "key_sheet_scanned": bool(session.exam_ids and session.keys and not session.imported_files
                                  and any(k.exam_id for k in session.keys.values())),
        "students": students,
        "flags": flags,
        "messages": [m.__dict__ for m in session.messages],
        "imported_files": session.imported_files,
        "exports": list(export.export_files(session)) + ["all_results.zip"] if session.keys else [],
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
def grade_key(file: UploadFile = File(...), version: int | None = Form(None),
              session: GradingSession = Depends(_session)):
    """Answer key upload. `version` says which version a plain CSV key is for;
    scanned key sheets and CSVs with Version columns identify versions themselves."""
    try:
        session.set_key_from_upload(file.filename or "key", _read_upload(file), version)
    except AnswerKeyError as exc:
        raise _bad(str(exc))
    return _state(session)


@app.post("/api/grade/import")
def grade_import(file: UploadFile = File(...), session: GradingSession = Depends(_session)):
    """Load or merge in a results CSV downloaded earlier from this site."""
    name = file.filename or "results.csv"
    if not name.lower().endswith((".csv", ".txt")):
        raise _bad(f"{name} is not a CSV file. Upload the results.csv you downloaded.")
    try:
        summary = session.import_results(name, _read_upload(file))
    except AnswerKeyError as exc:
        raise _bad(str(exc))
    return {"summary": summary, "state": _state(session)}


@app.post("/api/grade/upload")
def grade_upload(file: UploadFile = File(...), session: GradingSession = Depends(_session)):
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
    if not session.keys:
        raise _bad("Upload the answer key first.")
    if not any(s.answers is not None for s in session.students.values()):
        raise _bad("No student sheets have been graded yet. Upload at least one sheet.")
    session.done = True
    return _state(session)


class ResolveIn(BaseModel):
    student_index: int
    question: int
    answer: str = Field("", max_length=20)  # letters ("B", "AC", "" = blank); version number for question 0


@app.post("/api/grade/resolve")
def grade_resolve(body: ResolveIn, session: GradingSession = Depends(_session)):
    try:
        session.resolve(body.student_index, body.question, body.answer)
    except ValueError as exc:
        raise _bad(str(exc))
    return _state(session)


class WrittenIn(BaseModel):
    student_index: int
    number: int | None = None    # a written response box, or ...
    question: int | None = None  # ... an open-response question
    score: float | None = None


@app.post("/api/grade/written")
def grade_written(body: WrittenIn, session: GradingSession = Depends(_session)):
    """Save a hand-entered score (written response box or open question)."""
    try:
        if body.question is not None:
            session.set_open_score(body.student_index, body.question, body.score)
        elif body.number is not None:
            session.set_written_score(body.student_index, body.number, body.score)
        else:
            raise ValueError("Say which written response or open question this score is for.")
    except ValueError as exc:
        raise _bad(str(exc))
    return {"ok": True}


class SettingsIn(BaseModel):
    multi_mode: str | None = None
    num_versions: int | None = None


@app.post("/api/grade/settings")
def grade_settings(body: SettingsIn, session: GradingSession = Depends(_session)):
    try:
        if body.multi_mode is not None:
            session.set_multi_mode(body.multi_mode)
        if body.num_versions is not None:
            session.set_num_versions(body.num_versions)
    except (ValueError, AnswerKeyError) as exc:
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


@app.get("/api/grade/export/{name}")
def grade_export(name: str, inline: bool = False, session: GradingSession = Depends(_session)):
    if not session.keys or not any(s.answers is not None for s in session.students.values()):
        raise _bad("There are no graded sheets to export yet.")
    files = {**export.export_files(session),
             "all_results.zip": ("application/zip", export.all_exports_zip)}
    if name not in files:
        raise _bad("Unknown export.", 404)
    media_type, build = files[name]
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
