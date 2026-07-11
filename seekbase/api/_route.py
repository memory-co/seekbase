"""Tiny framework-free routing primitive shared by every endpoint module.

There is no FastAPI: each ``seekbase/api/<name>.py`` declares one ``Endpoint``
(method + path + async handler). ``server.py`` matches an incoming request
against the registered endpoints. The directory listing *is* the API surface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

# handler(db, body, params) -> (status_code, json_dict)
Handler = Callable[..., Awaitable[tuple[int, dict]]]


@dataclass(frozen=True)
class Endpoint:
    method: str          # "GET" / "POST"
    path: str            # may contain "{param}" segments, e.g. /v1/writes/{ticket}
    handle: Handler


def match_path(template: str, path: str) -> dict | None:
    """Return the captured ``{param}`` values if ``path`` matches ``template``,
    else ``None``. Segment-wise; no regex, no trailing-slash surprises."""
    t = template.strip("/").split("/")
    p = path.strip("/").split("/")
    if len(t) != len(p):
        return None
    params: dict[str, str] = {}
    for seg_t, seg_p in zip(t, p):
        if seg_t.startswith("{") and seg_t.endswith("}"):
            params[seg_t[1:-1]] = seg_p
        elif seg_t != seg_p:
            return None
    return params
