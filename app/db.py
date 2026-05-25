from supabase import create_client, Client
from flask import current_app
import functools


@functools.lru_cache(maxsize=1)
def _make_client(url: str, key: str) -> Client:
    return create_client(url, key)


def get_db() -> Client:
    return _make_client(
        current_app.config['SUPABASE_URL'],
        current_app.config['SUPABASE_SERVICE_ROLE_KEY'],
    )
