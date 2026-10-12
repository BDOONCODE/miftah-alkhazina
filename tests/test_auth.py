from app import models


def login(client, username, password="password123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def test_home_requires_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_wrong_password_rejected(client, make_user):
    make_user("acc")
    assert login(client, "acc", "wrong-pass").status_code == 401


def test_login_shows_only_assigned_entities(client, db, make_user):
    mine, other = models.Entity(name="مؤسسة النخيل"), models.Entity(name="شركة أخرى")
    db.add_all([mine, other])
    db.commit()
    make_user("acc", entities=[mine])

    assert login(client, "acc").status_code == 303
    page = client.get("/").text
    assert "مؤسسة النخيل" in page
    assert "شركة أخرى" not in page


def test_admin_gets_support_page_not_company_pages(client, db, make_user):
    """The general admin supports subscribers: no company data, IBANs or amounts."""
    entity = models.Entity(name="مؤسسة أ")
    db.add(entity)
    db.commit()
    make_user("boss", role=models.Role.ADMIN)
    login(client, "boss")
    assert client.get("/", follow_redirects=False).headers["location"] == "/admin/support"
    page = client.get("/admin/support").text
    assert "الدعم والتكامل" in page and "مؤسسة أ" in page
    for path in ("dashboard", "policy", "accounts", "transactions", "balances", "settings"):
        assert client.get(f"/entities/{entity.id}/{path}").status_code == 404, path
    assert client.get("/companies/new").status_code == 403


def test_support_page_is_admin_only(client, make_user):
    make_user("acc")
    login(client, "acc")
    assert client.get("/admin/support").status_code == 403


def test_inactive_user_cannot_login(client, db, make_user):
    user = make_user("gone")
    user.is_active = False
    db.commit()
    assert login(client, "gone", "wrong-pass").status_code == 401  # a wrong password reveals nothing
    response = login(client, "gone")
    assert response.status_code == 403 and "تم تعطيل حسابكم" in response.text
    assert client.get("/", follow_redirects=False).status_code == 303  # still not signed in


def test_disabled_user_writes_to_support_with_a_screenshot(client, db, make_user):
    user = make_user("gone")
    user.is_active = False
    db.commit()
    login(client, "gone")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    jpg = b"\xff\xd8\xff" + b"1" * 32
    response = client.post("/support", data={"subject": "حسابي معطل", "body": "أرجو إعادة التفعيل"},
                           files=[("images", ("a.png", png, "image/png")), ("images", ("b.jpg", jpg, "image/jpeg"))],
                           follow_redirects=False)
    assert response.status_code == 303
    ticket = db.query(models.SupportTicket).one()
    assert ticket.user_id == user.id and [(i.data, i.mime) for i in ticket.images] == [(png, "image/png"), (jpg, "image/jpeg")]
    assert client.post("/support", data={"subject": "x", "body": "y"},
                       files={"images": ("a.png", b"<script>", "image/png")}).status_code == 422  # not an image
    assert "جديد" in client.get("/support").text

    client.post("/logout")
    make_user("boss", role=models.Role.ADMIN)
    login(client, "boss")
    page = client.get("/admin/support").text
    assert "حسابي معطل" in page and "حساب معطّل" in page and "جديد" in page
    assert "أرجو إعادة التفعيل" in client.get(f"/admin/tickets/{ticket.id}").text
    db.refresh(ticket)
    assert ticket.status == "in_progress"  # opening it tells the sender it is being handled
    assert client.get(f"/admin/ticket-images/{ticket.images[0].id}").content == png
    client.post(f"/admin/tickets/{ticket.id}/close", data={"reply": "أعدنا التفعيل"})
    db.refresh(ticket)
    assert (ticket.status, ticket.reply) == ("closed", "أعدنا التفعيل")


def test_every_role_but_admin_reaches_support(client, db, make_user):
    for name, role in (("acc", models.Role.ACCOUNTANT), ("rev", models.Role.APPROVER)):
        make_user(name, role=role)
        login(client, name)
        page = client.get("/support").text
        assert "طلب دعم" in page and 'href="/support"' in page
        client.post("/logout")
    make_user("boss", role=models.Role.ADMIN)
    login(client, "boss")
    assert client.get("/support", follow_redirects=False).headers["location"] == "/admin/support#tickets"


def test_support_form_needs_a_known_sender(client):
    assert client.get("/support", follow_redirects=False).headers["location"] == "/login"


def test_logout_clears_session(client, make_user):
    make_user("acc")
    login(client, "acc")
    client.post("/logout")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_blocked_after_repeated_failures(client, make_user):
    from app import throttle

    throttle._failures.clear()
    make_user("acc")
    for _ in range(throttle.MAX_FAILURES):
        assert login(client, "acc", "wrong-pass").status_code == 401
    assert login(client, "acc").status_code == 429  # even the correct password is temporarily refused
    throttle._failures.clear()
    assert login(client, "acc").status_code == 303


def test_healthz_is_public_and_light(client):
    response = client.get("/healthz")
    assert (response.status_code, response.text) == (200, "ok")


def test_keepalive_touches_database(client):
    response = client.get("/keepalive")
    assert (response.status_code, response.text) == (200, "ok")


def test_username_is_case_insensitive(client, make_user):
    make_user("ahmed.arfaj")
    assert login(client, "Ahmed.Arfaj ").status_code == 303


def test_admin_resets_password(client, db, make_user):
    target = make_user("ahmed.arfaj", password="forgotten-pass")
    make_user("boss", role=models.Role.ADMIN)
    login(client, "boss")
    client.post(f"/admin/users/{target.id}/reset-password", data={"password": "New-temp-pass1!"})
    client.post("/logout")
    assert login(client, "ahmed.arfaj", "New-temp-pass1!").headers["location"] == "/account/password"


def test_non_admin_cannot_reset_password(client, make_user):
    target = make_user("victim")
    make_user("acc")
    login(client, "acc")
    assert client.post(f"/admin/users/{target.id}/reset-password", data={"password": "hijack-pass"}).status_code == 403


def test_admin_api_docs_and_no_accountant_creation(client, make_user):
    make_user("boss", role=models.Role.ADMIN)
    login(client, "boss")
    page = client.get("/admin/api").text
    assert "/api/bank/incoming" in page and "X-Signature" in page and "محاكاة" in page
    assert "إضافة محاسب" not in client.get("/admin/users").text
    assert client.post("/admin/users", data={"full_name": "x", "username": "x", "password": "Xx-123456!"}).status_code == 405
