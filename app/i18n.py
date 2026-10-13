"""Arabic / English interface.

Arabic is the source language: every Arabic string in the templates, the Python code and the scripts is
also a key in the English catalog (app/i18n_en.py). Translation happens when a page is rendered:
  - static template text and string literals are rewritten at compile time into `_td(...)` / `_ts(...)` calls
    (the ArabicText extension), which translate at render time;
  - every printed expression passes through `finalize`, which translates labels, flash messages and errors
    coming from Python (exact matches, or patterns with {} for messages built with values);
  - user data (company, item and account names) is never in the catalog, so it is printed as written.
    Values put back into form fields are marked with the `keep` filter so they are never translated.

The language lives in a long-lived cookie, so it survives sign-in and sign-out.
"""
from __future__ import annotations

import re
from contextvars import ContextVar

from jinja2.ext import Extension
from jinja2.lexer import Token
from markupsafe import Markup

LANGS = ("ar", "en")
COOKIE = "lang"
current_lang: ContextVar[str] = ContextVar("current_lang", default="ar")

HAS_ARABIC = re.compile(r"[؀-ۿ]")
# A run of Arabic text inside static template text: starts with an Arabic character and stops at markup,
# template syntax, quotes or the end of the line
ARABIC_RUN = re.compile(r"[؀-ۿ][^<>{}\"'\n`$;]*")
JOINERS = {"؛ ": "; ", "\n": "\n", "، ": ", "}


class Keep(str):
    """A value printed exactly as is (user data put back into a form field)."""


def _catalog():
    from .i18n_en import EN

    exact, patterns = {}, []
    for ar, en in EN.items():
        if "{}" in ar:
            parts = [re.escape(p) for p in ar.split("{}")]
            patterns.append((re.compile("(.+?)".join(parts), re.S), en, sum(len(p) for p in ar.split("{}"))))
        else:
            exact[ar] = en
    patterns.sort(key=lambda p: -p[2])  # the most specific pattern first
    return exact, [(rx, en) for rx, en, _ in patterns]


_EXACT: dict[str, str] | None = None
_PATTERNS: list = []


def _load() -> None:
    global _EXACT, _PATTERNS
    if _EXACT is None:
        _EXACT, _PATTERNS = _catalog()


def translate(text: str) -> str:
    """English for an Arabic string (exact, pattern, or joined parts); unknown text is returned as is."""
    _load()
    core = text.strip()
    if not core or not HAS_ARABIC.search(core):
        return text
    hit = _EXACT.get(core)
    if hit is not None:
        return text.replace(core, hit, 1)
    for rx, en in _PATTERNS:
        m = rx.fullmatch(core)
        if m:
            return text.replace(core, en.format(*(translate(g) for g in m.groups())), 1)
    for ar_sep, en_sep in JOINERS.items():
        if ar_sep in core:
            return text.replace(core, en_sep.join(translate(p) for p in core.split(ar_sep)), 1)
    return text


def t(text):
    """Translate for the current request's language."""
    if current_lang.get() != "en" or not isinstance(text, str) or isinstance(text, Keep):
        return text
    return translate(text)


def finalize(value):
    """Applied by Jinja to every printed expression."""
    if isinstance(value, str) and not isinstance(value, (Markup, Keep)) and current_lang.get() == "en":
        return translate(value)
    return value


def _td(text: str) -> Markup:
    """Static template text (trusted markup)."""
    return Markup(t(text))


def _ts(text: str) -> str:
    """A string literal inside a template expression."""
    return t(text)


def template_runs(text: str):
    """(start, end) of each Arabic run in static template text."""
    for m in ARABIC_RUN.finditer(text):
        run = m.group().rstrip()
        yield m.start(), m.start() + len(run)


class ArabicText(Extension):
    """Rewrites Arabic static text and string literals into translating calls when a template compiles."""

    def filter_stream(self, stream):
        for tok in stream:
            if tok.type == "data" and HAS_ARABIC.search(tok.value):
                pos = 0
                for start, end in template_runs(tok.value):
                    if start > pos:
                        yield Token(tok.lineno, "data", tok.value[pos:start])
                    yield Token(tok.lineno, "variable_begin", "{{")
                    yield Token(tok.lineno, "name", "_td")
                    yield Token(tok.lineno, "lparen", "(")
                    yield Token(tok.lineno, "string", tok.value[start:end])
                    yield Token(tok.lineno, "rparen", ")")
                    yield Token(tok.lineno, "variable_end", "}}")
                    pos = end
                if pos < len(tok.value):
                    yield Token(tok.lineno, "data", tok.value[pos:])
            elif tok.type == "string" and HAS_ARABIC.search(tok.value):
                yield Token(tok.lineno, "name", "_ts")
                yield Token(tok.lineno, "lparen", "(")
                yield tok
                yield Token(tok.lineno, "rparen", ")")
            else:
                yield tok


class LanguageMiddleware:
    """Sets the request's language from the cookie (pure ASGI, so the value reaches sync endpoints too)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        lang = "ar"
        for key, value in scope.get("headers", []):
            if key == b"cookie":
                m = re.search(rb"(?:^|;\s*)lang=(ar|en)\b", value)
                if m:
                    lang = m.group(1).decode()
        token = current_lang.set(lang)
        try:
            await self.app(scope, receive, send)
        finally:
            current_lang.reset(token)


def js_catalog() -> dict[str, str]:
    """The entries the scripts need (keys marked in i18n_en.JS_KEYS)."""
    from .i18n_en import EN, JS_KEYS

    return {k: EN[k] for k in JS_KEYS if k in EN} if current_lang.get() == "en" else {}
