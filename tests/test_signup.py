"""التسجيل الذاتي، تأكيد البريد، استعادة كلمة المرور، وشروط كلمة المرور."""
import re

import pytest

from app import mailer, models
from app.security import hash_password, password_problems

STRONG = "Strong-pass1!"


@pytest.fixture(autouse=True)
def outbox():
    mailer.outbox = []
    yield mailer.outbox
    mailer.outbox = None


def link_from(message) -> str:
    return re.search(r"(/(?:verify-email|reset-password)\?token=[\w.\-]+)", message["text"]).group(1)


def signup(client, **overrides):
    data = {
        "full_name": "أحمد العرفج",
        "email": "Ahmed@Example.com",
        "password": STRONG,
        "confirm": STRONG,
        "account_type": "business",
    } | overrides
    return client.post("/signup", data=data, follow_redirects=False)


def test_password_rules():
    assert password_problems(STRONG) == []
    assert len(password_problems("password123")) == 2  # بدون حرف كبير ولا رمز
    assert "رمز خاص مثل ! @ # $" in password_problems("Password123")


def test_signup_verify_and_login(client, db, outbox):
    response = signup(client)
    assert response.headers["location"] == "/signup/check-email"
    user = db.query(models.User).one()
    assert (user.email, user.username, user.email_verified, user.account_type) == (
        "ahmed@example.com", "ahmed@example.com", False, "business"
    )
    assert outbox[0]["to"] == "ahmed@example.com"

    # قبل التأكيد: الدخول ممنوع مع عرض خيار إعادة الإرسال
    blocked = client.post("/login", data={"username": "ahmed@example.com", "password": STRONG})
    assert blocked.status_code == 403 and "أرسل لي رابط جديد" in blocked.text

    verified = client.get(link_from(outbox[0]), follow_redirects=False)
    assert verified.headers["location"] == "/"
    db.refresh(user)
    assert user.email_verified
    assert client.get("/", follow_redirects=False).status_code == 200  # داخل بدون إعادة توجيه للدخول


def test_signup_rejects_weak_password_and_duplicates(client, db):
    assert signup(client, password="password123", confirm="password123").status_code == 422
    assert db.query(models.User).count() == 0
    signup(client)
    assert signup(client, email="ahmed@example.com").status_code == 422  # نفس البريد بحروف مختلفة
    assert signup(client, email="not-an-email").status_code == 422


def test_bad_verification_token(client):
    assert client.get("/verify-email?token=forged").status_code == 400


def test_resend_does_not_reveal_accounts(client, outbox):
    response = client.post("/verify-email/resend", data={"email": "nobody@example.com"})
    assert "لو البريد مسجّل" in response.text
    assert outbox == []


def test_forgot_password_link_is_single_use(client, db, outbox):
    signup(client)
    client.get(link_from(outbox[0]))  # تأكيد البريد
    client.post("/logout")

    page = client.post("/forgot-password", data={"login": "AHMED@example.com"})
    assert "لو الحساب مسجّل" in page.text
    reset_link = link_from(outbox[-1])

    assert client.post(
        "/reset-password",
        data={"token": reset_link.split("token=")[1], "password": "weak", "confirm": "weak"},
    ).status_code == 422
    new = "Brand-new-pass2#"
    done = client.post(
        "/reset-password", data={"token": reset_link.split("token=")[1], "password": new, "confirm": new},
        follow_redirects=False,
    )
    assert done.headers["location"] == "/login"
    assert client.post("/login", data={"username": "ahmed@example.com", "password": new}, follow_redirects=False).status_code == 303
    # نفس الرابط ما يشتغل مرة ثانية
    assert client.get(reset_link).status_code == 400


def test_forgot_password_unknown_account_sends_nothing(client, outbox):
    client.post("/forgot-password", data={"login": "ghost@example.com"})
    assert outbox == []


def test_self_signed_user_can_register_company_with_self_approval(client, db, outbox):
    signup(client, account_type="office")
    client.get(link_from(outbox[0]))
    response = client.post("/companies/new", data={"name": "شركة العميل", "approval_mode": "self"}, follow_redirects=False)
    assert response.status_code == 303
    assert db.query(models.Entity).filter_by(name="شركة العميل").one().approval_mode == "self"


def test_arabic_letters_are_not_symbols():
    assert "رمز خاص مثل ! @ # $" in password_problems("يبيسيصصصصضض")
    assert password_problems("Strongpass1ع!") == ["إنجليزي فقط (بدون عربي أو مسافات)"]
    assert password_problems("Strong pass1!") == ["إنجليزي فقط (بدون عربي أو مسافات)"]


def test_admin_can_verify_email_manually(client, db, make_user):
    signup(client)
    user = db.query(models.User).filter_by(email="ahmed@example.com").one()
    make_user("boss", role=models.Role.ADMIN)
    client.post("/login", data={"username": "boss", "password": "password123"})
    assert "بانتظار تأكيد البريد" in client.get("/admin/users").text
    client.post(f"/admin/users/{user.id}/verify-email")
    db.refresh(user)
    assert user.email_verified


@pytest.fixture
def mail_not_configured(monkeypatch):
    monkeypatch.setattr(mailer, "outbox", None)
    monkeypatch.delenv("BREVO_API_KEY", raising=False)
    monkeypatch.delenv("MAIL_FROM", raising=False)


def test_without_email_service_signup_logs_in_directly(client, db, mail_not_configured):
    response = signup(client)
    assert response.headers["location"] == "/"
    assert db.query(models.User).one().email_verified
    assert client.get("/", follow_redirects=False).status_code == 200


def test_without_email_service_stuck_account_gets_in(client, db, mail_not_configured):
    user = models.User(
        username="old@example.com", email="old@example.com", full_name="قديم", role=models.Role.ACCOUNTANT,
        password_hash=hash_password(STRONG), email_verified=False,
    )
    db.add(user)
    db.commit()
    response = client.post("/login", data={"username": "old@example.com", "password": STRONG}, follow_redirects=False)
    assert response.status_code == 303
    db.refresh(user)
    assert user.email_verified
    assert "غير متاحة حاليًا" in client.get("/forgot-password").text
