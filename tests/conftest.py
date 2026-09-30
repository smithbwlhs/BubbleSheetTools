"""Test settings: skip Supabase by using the local development login bypass."""

import os

os.environ["AUTH_DEV_BYPASS"] = "1"
os.environ["ENVIRONMENT"] = "test"
os.environ.pop("SUPABASE_URL", None)
