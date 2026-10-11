from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import mailer, throttle
from .auth import LoginRequired, PasswordChangeRequired, accessible_entities, current_user
from .config import BASE_DIR, HTTPS_ONLY_COOKIES, SECRET_KEY
from .db import get_session
from .models import AuditLog, User, utcnow
from .routes import account, accounts, bank, companies, dashboard, payouts, policies, reports, signup, transactions
from .security import verify_password
from .services.policies import active_policy, open_policy
from .services.signup import find_by_login
from .templating import templates
from .web import redirect

def _self_ping() -> None:
    """The free host sleeps the site after 15 idle minutes, and the first visit after that takes about a minute.
    So the site visits its own public URL every 10 minutes while it runs, and never reaches sleep.
    (The visit also runs any payouts that are due.) Locally it does nothing because there is no public URL."""
    import os
    import threading
    import time
    import urllib.request

    base = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("APP_URL") or "").rstrip("/")
    if not base.startswith("https://"):
        return

    def loop() -> None:
        while True:
            time.sleep(10 * 60)
            try:
                urllib.request.urlopen(f"{base}/keepalive", timeout=60).read()
            except Exception as exc:  # temporary outage: retry in 10 minutes
                logging.getLogger(__name__).warning("self-ping failed: %s", exc)

    threading.Thread(target=loop, name="self-ping", daemon=True).start()


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    _self_ping()
    yield


app = FastAPI(title="مفتاح الخزينة", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.add_middleware(
    SessionMiddleware, secret_key=SECRET_KEY, https_only=HTTPS_ONLY_COOKIES, same_site="lax", max_age=12 * 3600
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.include_router(signup.router)
app.include_router(account.router)
app.include_router(companies.router)
app.include_router(accounts.router)
app.include_router(bank.router)
app.include_router(policies.router)
app.include_router(transactions.router)
app.include_router(payouts.router)
app.include_router(dashboard.router)
app.include_router(reports.router)


class _HideHealthChecks(logging.Filter):
    """Health and keep-alive checks repeat often and clutter the log, so they are not logged."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return "/healthz" not in message and "/keepalive" not in message


logging.getLogger("uvicorn.access").addFilter(_HideHealthChecks())


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Render health check (every 5 seconds): lightweight, no database or pages."""
    return PlainTextResponse("ok")


@app.get("/keepalive", include_in_schema=False)
def keepalive(session: Session = Depends(get_session)):
    """Visited by an external monitor every 5 minutes: keeps the free host awake,
    and a trivial query keeps Supabase from pausing the database after a week of inactivity."""
    session.execute(text("SELECT 1"))
    # A regular chance to run due payouts (the free host has no task scheduler)
    from .domain.periods import local_date
    from .services.payouts import run_due_all

    if run_due_all(session, local_date(utcnow())):
        session.commit()
    return PlainTextResponse("ok")


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, _exc: LoginRequired):
    return redirect("/login")


@app.exception_handler(PasswordChangeRequired)
async def _password_change_required(request: Request, _exc: PasswordChangeRequired):
    return redirect("/account/password")


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    return templates.TemplateResponse(
        request, "error.html", {"user": None, "status": exc.status_code, "detail": exc.detail}, status_code=exc.status_code
    )


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    ip = request.client.host if request.client else "?"
    if throttle.is_blocked(ip, username):
        return templates.TemplateResponse(
            request, "login.html", {"error": "محاولات دخول كثيرة. حاول بعد ١٠ دقائق"}, status_code=429
        )
    user = find_by_login(session, username)
    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        throttle.record_failure(ip, username)
        return templates.TemplateResponse(
            request, "login.html", {"error": "البريد/اسم المستخدم أو كلمة المرور غير صحيحة"}, status_code=401
        )
    throttle.reset(ip, username)
    if not user.email_verified and not mailer.is_configured():
        # Registered before email sending was enabled: the confirmation link never arrived, so verify after a correct password
        user.email_verified = True
    if not user.email_verified:
        # Only reveal that the email is unverified after a correct password
        return templates.TemplateResponse(
            request, "login.html", {"error": None, "unverified_email": user.email}, status_code=403
        )
    request.session.clear()
    request.session["user_id"] = user.id
    session.add(AuditLog(user_id=user.id, action="user.login"))
    session.commit()
    return redirect("/account/password" if user.must_change_password else "/")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


@app.get("/", response_class=HTMLResponse)
def home(request: Request, user: User = Depends(current_user), session: Session = Depends(get_session)):
    rows = [
        {"entity": e, "active": active_policy(session, e.id), "open": open_policy(session, e.id)}
        for e in accessible_entities(user, session)
    ]
    return templates.TemplateResponse(request, "home.html", {"user": user, "rows": rows})
