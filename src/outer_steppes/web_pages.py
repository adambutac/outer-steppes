"""Every HTML page the bridge serves, and the headers they all carry (DESIGN.md, Pages).

Pages are static: no JavaScript and no external resources. `render` escapes
its plain-text message; `render_html` takes a body built by the caller, which
must escape every dynamic value itself (with `esc`). The account pages, the
login pages that link back to them, and the connect-time warning use `render_html`.
"""

from __future__ import annotations

import html
from collections.abc import Iterable

from aiohttp import web

STYLE = ("body{font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem;"
         "line-height:1.5;color:#1b1b1b;background:#fff}"
         "@media (prefers-color-scheme:dark){body{color:#e8e8e8;background:#161616}"
         "input{background:#222;color:#e8e8e8;border-color:#555}a{color:#8ab4f8}}"
         "h1{font-size:1.25rem}h2{font-size:1.05rem;margin-top:2rem}"
         "label{display:block;margin-top:.8rem}input[type=text],input[type=password]{width:100%;"
         "padding:.45rem;box-sizing:border-box;border:1px solid #999;border-radius:4px}"
         "button{margin-top:1rem;padding:.5rem 1rem}code{word-break:break-all}"
         "pre{white-space:pre-wrap;font-size:.9rem}.error{color:#b00020}"
         "@media (prefers-color-scheme:dark){.error{color:#ff8a80}}"
         "td{padding:.15rem .6rem .15rem 0;vertical-align:top}"
         ".choice a{display:inline-block;margin:1rem 1rem 0 0;padding:.5rem 1rem}"
         ".choice a.main{font-weight:bold;border:2px solid currentColor;border-radius:4px;text-decoration:none}")

# Account pages: under "no-referrer", browsers send `Origin: null` on form POSTs,
# which would defeat the Origin check (DESIGN.md, The account page).
REFERRER_ACCOUNT = "same-origin"

esc = html.escape


def page_headers(form_action_origins: Iterable[str] = (), referrer_policy: str = "no-referrer") -> dict[str, str]:
    """The headers on every bridge page. `form_action_origins` extends form-action beyond 'self'."""
    form_action = " ".join(["'self'", *sorted(set(form_action_origins))])
    return {
        "Content-Security-Policy": (f"default-src 'none'; style-src 'unsafe-inline'; form-action {form_action}; "
                                    "frame-ancestors 'none'"),
        "Cache-Control": "no-store",
        "Referrer-Policy": referrer_policy,
        "X-Content-Type-Options": "nosniff",
    }


def render(status: int, title: str, message: str, form_action_origins: Iterable[str] = (),
           referrer_policy: str = "no-referrer") -> web.Response:
    """A plain-text message as a page. `title` and `message` are escaped."""
    return render_html(status, title, f"<p>{esc(message)}</p>", form_action_origins, referrer_policy)


def render_html(status: int, title: str, body_html: str, form_action_origins: Iterable[str] = (),
                referrer_policy: str = "no-referrer") -> web.Response:
    """`body_html` is trusted markup: every dynamic value in it must already be escaped."""
    doc = (f"<!doctype html><html lang=en><meta charset=utf-8>"
           f"<meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>{esc(title)}</title><style>{STYLE}</style>"
           f"<h1>{esc(title)}</h1>{body_html}</html>")
    return web.Response(status=status, text=doc, content_type="text/html", charset="utf-8",
                        headers=page_headers(form_action_origins, referrer_policy))
