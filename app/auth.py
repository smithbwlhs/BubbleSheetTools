"""
Teacher accounts via Supabase Auth.

Only the grading features require an account; creating bubble sheets is open.

Design notes
  * The browser never talks to Supabase and never sees any Supabase key.
    This server calls Supabase's Auth REST API with keys read from the
    environment (see config.py).
  * The login tokens Supabase returns are kept in HttpOnly cookies, so page
    JavaScript cannot read them.
  * The only personal data stored (by Supabase) is the teacher's email,
    first name, last name and their confirmation that they are 18 or older.
    Student information is never sent to Supabase.
"""

import time

import httpx
from fastapi import HTTPException, Request, Response

from .config import settings

ACCESS_COOKIE = "bst_access"
REFRESH_COOKIE = "bst_refresh"
_TIMEOUT = httpx.Timeout(10.0)

# Short cache of verified tokens -> user, so every request doesn't hit Supabase.
_token_cache: dict[str, tuple[float, dict]] = {}
_CACHE_SECONDS = 60


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _configured() -> None:
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise AuthError("Sign-in is not configured on this server yet.", 503)


def _headers(access_token: str | None = None) -> dict:
    """Supabase request headers. The project key goes in `apikey` (works for both
    legacy "anon" keys and newer "publishable" keys); a user's token, when there
    is one, goes in Authorization."""
    headers = {"apikey": settings.supabase_anon_key, "Content-Type": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def _error_message(resp: httpx.Response, default: str) -> str:
    """Pull a readable message out of a Supabase error response."""
    try:
        body = resp.json()
    except ValueError:
        return default
    return body.get("msg") or body.get("error_description") or body.get("message") or default


def _user_view(user: dict) -> dict:
    """The only fields of a Supabase user this app ever uses."""
    meta = user.get("user_metadata") or {}
    return {
        "id": user["id"],
        "email": user.get("email", ""),
        "first_name": meta.get("first_name", ""),
        "last_name": meta.get("last_name", ""),
    }


# ------------------------------------------------------------ actions ----

def sign_up(first_name: str, last_name: str, email: str, password: str,
            is_adult: bool, redirect_to: str) -> None:
    """Create an account. Supabase emails a confirmation link."""
    first_name, last_name, email = first_name.strip(), last_name.strip(), email.strip()
    if not (first_name and last_name and email and password):
        raise AuthError("Please fill in first name, last name, email and password.")
    if not is_adult:
        raise AuthError("You must confirm that you are 18 or older to create an account.")
    if len(password) < 8:
        raise AuthError("Passwords must be at least 8 characters.")
    if len(first_name) > 60 or len(last_name) > 60:
        raise AuthError("Names must be 60 characters or fewer.")
    _configured()
    resp = httpx.post(
        f"{settings.supabase_url}/auth/v1/signup",
        params={"redirect_to": redirect_to},
        headers=_headers(),
        json={
            "email": email,
            "password": password,
            "data": {
                "first_name": first_name,
                "last_name": last_name,
                "age_confirmed_18_plus": True,
            },
        },
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise AuthError(_error_message(resp, "Could not create the account."))


def log_in(email: str, password: str, response: Response) -> dict:
    """Check credentials with Supabase and set the session cookies."""
    if not email.strip() or not password:
        raise AuthError("Please enter your email and password.")
    _configured()
    resp = httpx.post(
        f"{settings.supabase_url}/auth/v1/token",
        params={"grant_type": "password"},
        headers=_headers(),
        json={"email": email.strip(), "password": password},
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 400:
        msg = _error_message(resp, "Incorrect email or password.")
        if "confirm" in msg.lower():
            msg = "Please confirm your email address first (check your inbox)."
        raise AuthError(msg, 401)
    return _store_tokens(resp.json(), response)


def log_out(request: Request, response: Response) -> None:
    token = request.cookies.get(ACCESS_COOKIE)
    if token and settings.supabase_url:
        try:
            httpx.post(f"{settings.supabase_url}/auth/v1/logout",
                       headers=_headers(token), timeout=_TIMEOUT)
        except httpx.HTTPError:
            pass  # clearing cookies below is what matters
        _token_cache.pop(token, None)
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/")


def send_password_reset(email: str, redirect_to: str) -> None:
    if not email.strip():
        raise AuthError("Please enter your email address.")
    _configured()
    httpx.post(f"{settings.supabase_url}/auth/v1/recover", params={"redirect_to": redirect_to},
               headers=_headers(), json={"email": email.strip()}, timeout=_TIMEOUT)
    # Always report success so this can't be used to discover who has an account.


def update_password(access_token: str, new_password: str) -> None:
    """Set a new password using the token from a password-reset email link."""
    if len(new_password) < 8:
        raise AuthError("Passwords must be at least 8 characters.")
    _configured()
    resp = httpx.put(f"{settings.supabase_url}/auth/v1/user", headers=_headers(access_token),
                     json={"password": new_password}, timeout=_TIMEOUT)
    if resp.status_code >= 400:
        raise AuthError(_error_message(resp, "The reset link has expired. Request a new one."))


# ----------------------------------------------------- current user ----

def _store_tokens(body: dict, response: Response) -> dict:
    """Put Supabase tokens into HttpOnly cookies and return the user."""
    cookie = dict(httponly=True, secure=settings.secure_cookies, samesite="lax", path="/")
    response.set_cookie(ACCESS_COOKIE, body["access_token"],
                        max_age=int(body.get("expires_in", 3600)), **cookie)
    response.set_cookie(REFRESH_COOKIE, body["refresh_token"], max_age=60 * 60 * 24 * 7, **cookie)
    user = _user_view(body["user"])
    _token_cache[body["access_token"]] = (time.time() + _CACHE_SECONDS, user)
    return user


def _fetch_user(token: str) -> dict | None:
    cached = _token_cache.get(token)
    if cached and cached[0] > time.time():
        return cached[1]
    try:
        resp = httpx.get(f"{settings.supabase_url}/auth/v1/user", headers=_headers(token),
                         timeout=_TIMEOUT)
    except httpx.HTTPError:
        raise AuthError("Could not reach the sign-in service. Try again shortly.", 503)
    if resp.status_code != 200:
        _token_cache.pop(token, None)
        return None
    user = _user_view(resp.json())
    _token_cache[token] = (time.time() + _CACHE_SECONDS, user)
    return user


def _refresh(refresh_token: str, response: Response) -> dict | None:
    try:
        resp = httpx.post(f"{settings.supabase_url}/auth/v1/token",
                          params={"grant_type": "refresh_token"}, headers=_headers(),
                          json={"refresh_token": refresh_token}, timeout=_TIMEOUT)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    return _store_tokens(resp.json(), response)


def current_user(request: Request, response: Response) -> dict | None:
    """The signed-in teacher, or None. Refreshes an expired login if possible."""
    if settings.auth_dev_bypass:
        return {"id": "dev-user", "email": "dev@localhost", "first_name": "Dev",
                "last_name": "Teacher"}
    if not settings.supabase_url:
        return None
    token = request.cookies.get(ACCESS_COOKIE)
    if token:
        user = _fetch_user(token)
        if user:
            return user
    refresh = request.cookies.get(REFRESH_COOKIE)
    if refresh:
        return _refresh(refresh, response)
    return None


def require_user(request: Request, response: Response) -> dict:
    """FastAPI dependency for routes that need a signed-in teacher."""
    try:
        user = current_user(request, response)
    except AuthError as exc:
        raise HTTPException(exc.status, str(exc)) from None
    if not user:
        raise HTTPException(401, "Please sign in to grade exams.")
    return user
