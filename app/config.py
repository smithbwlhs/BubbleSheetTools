"""
Application settings, read from environment variables.

Secrets (Supabase keys) are ONLY ever read from the environment on the
server. They are never sent to the browser and must never be committed.
For local development, copy `.env.example` to `.env` (which is git-ignored).
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load a local .env file if one exists. In production the variables are
# supplied by Docker / the host instead, and this is a no-op.
load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    """Read an environment variable as a boolean ("1", "true", "yes" = True)."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Supabase project URL, e.g. https://abcd1234.supabase.co
    supabase_url: str = os.getenv("SUPABASE_URL", "").rstrip("/")
    # Supabase "publishable" key (called the "anon" key in older projects).
    # Only used server-side here; it is never sent to the browser.
    supabase_anon_key: str = os.getenv("SUPABASE_ANON_KEY", "")

    # LOCAL DEVELOPMENT ONLY: skip Supabase and treat every request to the
    # grading API as a logged-in test user. Refused when ENVIRONMENT=production.
    auth_dev_bypass: bool = _bool("AUTH_DEV_BYPASS")
    environment: str = os.getenv("ENVIRONMENT", "development")

    # Set cookies with the Secure flag (HTTPS only). On by default in production.
    secure_cookies: bool = _bool("SECURE_COOKIES", os.getenv("ENVIRONMENT") == "production")

    # How long an in-memory grading session lives without activity (minutes).
    session_ttl_minutes: int = int(os.getenv("SESSION_TTL_MINUTES", "120"))

    # Upload limits, to protect the server from huge files.
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "40"))
    max_pages_per_upload: int = int(os.getenv("MAX_PAGES_PER_UPLOAD", "200"))


settings = Settings()

if settings.auth_dev_bypass and settings.environment == "production":
    raise RuntimeError("AUTH_DEV_BYPASS must never be enabled in production.")
