"""Arabic / English interface: the toggle, the page direction, and full coverage of the template text."""
import pathlib

from jinja2 import Environment
from jinja2.lexer import get_lexer

from app.i18n import HAS_ARABIC, current_lang, t, template_runs, translate
from app.i18n_en import EN, JS_KEYS

# Shown as is in both languages
UNTRANSLATED = {"العربية"}


def login(client, username, password="password123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def test_every_template_text_has_an_english_translation():
    env, missing = Environment(), set()
    for path in pathlib.Path("app/templates").rglob("*.html"):
        for tok in get_lexer(env).tokenize(path.read_text(encoding="utf-8"), str(path)):
            texts = [tok.value[s:e] for s, e in template_runs(tok.value)] if tok.type == "data" else (
                [tok.value] if tok.type == "string" else [])
            for text in texts:
                if HAS_ARABIC.search(text) and text.strip() not in UNTRANSLATED and translate(text) == text:
                    missing.add(f"{path.name}: {text.strip()}")
    assert not missing, sorted(missing)


def test_script_keys_exist_and_translations_are_attribute_safe():
    assert all(k in EN for k in JS_KEYS)
    # Some translations land inside HTML attributes or script strings
    assert not [v for k, v in EN.items() if "'" in v or ('"' in v and not k.startswith("<div"))]


def test_patterns_fill_values_and_keep_user_data():
    token = current_lang.set("en")
    try:
        assert t("تم حذف الحساب «نقاط البيع»") == "The account “نقاط البيع” was deleted"
        assert t("شركة تجريبية") == "شركة تجريبية"  # user data is not in the catalog
    finally:
        current_lang.reset(token)
    assert t("تم حذف المسودة") == "تم حذف المسودة"  # Arabic is the default


def test_toggle_switches_the_whole_page(client, make_user):
    page = client.get("/login").text
    assert 'dir="rtl"' in page and "سجّل دخولك للمتابعة" in page and ">English<" in page

    response = client.get("/lang/en?next=/login", follow_redirects=False)
    assert response.headers["location"] == "/login" and "lang=en" in response.headers["set-cookie"]
    page = client.get("/login").text
    assert 'dir="ltr"' in page and "Sign in to continue" in page and ">العربية<" in page
    assert "سجّل" not in page

    make_user("acc")
    login(client, "acc")  # the language survives signing in
    page = client.get("/").text
    assert 'lang="en"' in page and "Register company" in page

    client.get("/lang/ar?next=/")
    assert "تسجيل شركة" in client.get("/").text


def test_toggle_only_returns_to_this_site(client):
    assert client.get("/lang/en?next=//evil.example", follow_redirects=False).headers["location"] == "/"
    assert client.get("/lang/en?next=https://evil.example", follow_redirects=False).headers["location"] == "/"


def test_errors_and_flashes_follow_the_language(client, db, make_user):
    client.get("/lang/en")
    assert "Incorrect email/username or password" in login(client, "nobody", "wrong").text
    user = make_user("gone")
    user.is_active = False
    db.commit()
    assert "Your account has been disabled." in login(client, "gone").text
