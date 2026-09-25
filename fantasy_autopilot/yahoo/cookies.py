"""Load a logged-in Yahoo session from whatever the user can copy out of a browser.

Accepted formats:
* the raw ``Cookie:`` request header ("A1=...; A3=...; T=...; Y=...")
* JSON exported by Cookie-Editor / EditThisCookie, Selenium ``get_cookies()``
  or a Playwright ``storageState`` file
* Netscape ``cookies.txt``
* any of the above base64-encoded (handy for a GitHub secret)

Cookies are credentials: they give full access to the Yahoo account. They are
never written to disk by this package.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Union

YAHOO_DOMAIN = ".yahoo.com"
CookieSource = Union[str, list, dict]


def _maybe_file(raw: str) -> str:
    s = raw.strip()
    if len(s) < 400 and "\n" not in s and "=" not in s:
        p = Path(s).expanduser()
        if p.is_file():
            return p.read_text()
    return raw


def _maybe_base64(raw: str) -> str:
    s = "".join(raw.split())
    if len(s) < 40 or not re.fullmatch(r"[A-Za-z0-9+/=_-]+", s):
        return raw
    try:
        decoded = base64.b64decode(s + "=" * (-len(s) % 4), altchars=b"-_" if "-" in s or "_" in s else None)
        text = decoded.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return raw
    return text if text.lstrip().startswith(("[", "{", "#")) or "\t" in text else raw


def _from_json_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for c in items:
        if not isinstance(c, dict) or "name" not in c:
            continue
        expires = c.get("expires", c.get("expirationDate", c.get("expiry")))
        out.append(
            {
                "name": str(c["name"]),
                "value": str(c.get("value", "")),
                "domain": c.get("domain") or YAHOO_DOMAIN,
                "path": c.get("path") or "/",
                "expires": float(expires) if isinstance(expires, (int, float)) and expires > 0 else None,
                "secure": bool(c.get("secure", True)),
                "httpOnly": bool(c.get("httpOnly", False)),
            }
        )
    return out


def parse_cookies(source: CookieSource) -> list[dict[str, Any]]:
    """Normalize any supported format to a list of cookie dicts."""
    if isinstance(source, dict):
        source = source.get("cookies", [])
    if isinstance(source, list):
        return _from_json_items(source)
    raw = _maybe_base64(_maybe_file(str(source))).strip()
    if not raw:
        return []
    if raw[0] in "[{":
        data = json.loads(raw)
        return parse_cookies(data)
    lines = [
        ln for ln in raw.splitlines()
        if (ln.strip() and not ln.startswith("#")) or ln.startswith("#HttpOnly_")
    ]
    if any(ln.count("\t") >= 6 for ln in lines):
        out = []
        for ln in lines:
            http_only = ln.startswith("#HttpOnly_")
            parts = ln.replace("#HttpOnly_", "", 1).split("\t")
            if len(parts) < 7:
                continue
            domain, _, path, secure, expires, name, value = parts[:7]
            out.append({
                "name": name, "value": value.strip(), "domain": domain, "path": path or "/",
                "expires": float(expires) if expires.strip().isdigit() and int(expires) > 0 else None,
                "secure": secure.upper() == "TRUE", "httpOnly": http_only,
            })
        return out
    header = re.sub(r"^\s*cookie\s*:\s*", "", raw, flags=re.I)
    out = []
    for part in header.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            out.append({"name": name, "value": value.strip(), "domain": YAHOO_DOMAIN, "path": "/",
                        "expires": None, "secure": True, "httpOnly": False})
    return out


def cookie_header(cookies: list[dict[str, Any]]) -> str:
    """Header for requests to football.fantasysports.yahoo.com."""
    yahoo = [c for c in cookies if "yahoo.com" in (c.get("domain") or YAHOO_DOMAIN)]
    seen: dict[str, str] = {}
    for c in yahoo or cookies:  # a file with other sites' cookies: keep only Yahoo's
        seen[c["name"]] = c["value"]
    return "; ".join(f"{k}={v}" for k, v in seen.items())


def to_playwright(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for c in cookies:
        item = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain") or YAHOO_DOMAIN,
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", False)),
            "sameSite": "Lax",
        }
        if c.get("expires"):
            item["expires"] = float(c["expires"])
        out.append(item)
    return out


def describe(cookies: list[dict[str, Any]]) -> str:
    """Safe summary for logs: names only, never values."""
    names = sorted({c["name"] for c in cookies})
    key = [n for n in ("T", "Y", "A1", "A3", "A1S") if n in names]
    return f"{len(cookies)} cookies (clave: {', '.join(key) or 'ninguna de T/Y/A1/A3'})"
