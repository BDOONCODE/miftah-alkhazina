from __future__ import annotations

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from . import throttle
from .auth import LoginRequired, PasswordChangeRequired, accessible_entities, current_user
from .config import BASE_DIR, HTTPS_ONLY_COOKIES, SECRET_KEY
from .db import get_session
from .models import AuditLog, User
from .routes import account, companies, dashboard, policies, reports, transactions
from .security import verify_password
from .services.policies import active_policy, open_policy
from .templating import templates
from .web import redirect

app = FastAPI(title="مفتاح الخزينة", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware, secret_key=SECRET_KEY, https_only=HTTPS_ONLY_COOKIES, same_site="lax", max_age=12 * 3600
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
app.include_router(account.router)
app.include_router(companies.router)
app.include_router(policies.router)
app.include_router(transactions.router)
app.include_router(dashboard.router)
app.include_router(reports.router)


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
    user = session.scalar(select(User).where(User.username == username.strip()))
    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        throttle.record_failure(ip, username)
        return templates.TemplateResponse(
            request, "login.html", {"error": "اسم المستخدم أو كلمة المرور غير صحيحة"}, status_code=401
        )
    throttle.reset(ip, username)
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
