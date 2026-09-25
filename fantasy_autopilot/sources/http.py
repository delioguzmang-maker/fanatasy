"""Shared HTTP session factory (retries, browser-like User-Agent)."""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


def make_session(user_agent: str = BROWSER_UA, retries: int = 3) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"})
    retry = Retry(
        total=retries,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
