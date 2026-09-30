"""الحسابات البنكية، وجهات البنود، والإيداع التلقائي (محاكي البنك وإشعار البنك)."""
import json

import pytest

from app import models
from app.models import AccountKind, TransactionSource
from app.routes.bank import sign
from app.services import accounts as acc
from app.services import companies
from app.services import policies as pol

POOL = "SA0380000000608010167519"
POS = "SA4420000001234567891234"
TAX = "SA1110000000000000000001"
LANDLORD = "SA2220000000000000000002"
SECRET = "test-webhook-secret"


@pytest.fixture
def company(client, db, make_user):
    make_user("acc")
    client.post("/login", data={"username": "acc", "password": "password123"})
    client.post("/companies/new", data={"name": "مطعم الريم", "approval_mode": "self"})
    return db.query(models.Entity).one()


def add(client, entity, name, iban, kind):
    return client.post(f"/entities/{entity.id}/accounts", data={"name": name, "iban": iban, "kind": kind}, follow_redirects=False)


@pytest.fixture
def accounts(client, db, company):
    add(client, company, "المجمّع", POOL, "pool")
    add(client, company, "نقاط البيع", POS, "source")
    add(client, company, "حساب الضريبة", TAX, "sub")
    add(client, company, "المؤجر", LANDLORD, "external")
    return {a.name: a for a in db.query(models.BankAccount).all()}


def activate_policy(client, db, company, accounts):
    client.post(f"/entities/{company.id}/policy/draft")
    draft = pol.open_policy(db, company.id)
    form = {
        "bucket_id": ["", ""],
        "name": ["الضريبة", "الإيجار"],
        "priority": ["1", "2"],
        "calc_type": ["percentage", "fixed_amount"],
        "value": ["15", "10000"],
        "frequency": ["immediate", "immediate"],  # بدون موعد: النتيجة ما تعتمد على تاريخ اليوم
        "settlement_day": ["", ""],
        "destination": [str(accounts["حساب الضريبة"].id), str(accounts["المؤجر"].id)],
        "protected": ["1", "0"],
        "notes": "",
        "justification": "",
        "intent": "submit",
    }
    client.post(f"/policies/{draft.id}/edit", data=form)
    db.refresh(draft)
    return draft


def test_accounts_validation(client, db, company):
    assert add(client, company, "المجمّع", "SA 0380 0000 0060 8010 1675 19", "pool").status_code == 303
    assert db.query(models.BankAccount).one().iban == POOL  # المسافات تنشال
    add(client, company, "مجمّع ثاني", POS, "pool")  # مجمّع واحد بس
    add(client, company, "مكرر", POOL, "source")  # نفس الآيبان
    add(client, company, "غلط", "12345", "source")
    assert db.query(models.BankAccount).count() == 1
    assert "مجمّع" in client.get(f"/entities/{company.id}/accounts").text


def test_policy_destination_is_an_account(client, db, company, accounts):
    policy = activate_policy(client, db, company, accounts)
    assert policy.status is models.PolicyStatus.ACTIVE
    by_name = {b.name: b for b in policy.buckets}
    assert by_name["الضريبة"].destination_account_id == accounts["حساب الضريبة"].id
    assert by_name["الإيجار"].destination == "المؤجر"
    edit_page = client.get(f"/entities/{company.id}/policy").text
    assert "المؤجر" in edit_page


def test_destination_must_be_sub_or_external(db, company, accounts):
    user = db.query(models.User).filter_by(username="acc").one()
    draft = pol.start_draft(db, company, user)
    bad = pol.BucketInput(None, "الضريبة", 1, models.CalcType.PERCENTAGE, 1500, models.Frequency.IMMEDIATE, None, "", False,
                          destination_account_id=accounts["نقاط البيع"].id)
    with pytest.raises(pol.PolicyError, match="وجهة"):
        pol.save_draft(db, draft, [bad], "", user)


def test_manual_entry_records_source(client, db, company, accounts):
    activate_policy(client, db, company, accounts)
    client.post(
        f"/entities/{company.id}/transactions",
        data={"amount": "1000", "occurred_at": "2026-09-02T10:00", "source_account_id": str(accounts["نقاط البيع"].id)},
    )
    txn = db.query(models.Transaction).one()
    assert (txn.source, txn.source_account_id) == (TransactionSource.MANUAL, accounts["نقاط البيع"].id)


def test_simulator_allocates_like_the_bank(client, db, company, accounts):
    activate_policy(client, db, company, accounts)
    response = client.post(
        f"/entities/{company.id}/bank-simulator",
        data={"account_id": str(accounts["نقاط البيع"].id), "amount": "2000"},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    txn = db.get(models.Transaction, response.json()["transaction_id"])
    assert txn.source is TransactionSource.OPEN_BANKING and txn.created_by is None
    assert txn.external_ref.startswith("SIM-")
    assert {e.allocated for e in txn.events} == {300_00, 1_700_00}  # ١٥٪ ضريبة، والباقي للإيجار
    page = client.get(f"/transactions/{txn.id}").text
    assert "البنك (تلقائي)" in page and "نقاط البيع" in page

    random_amount = client.post(
        f"/entities/{company.id}/bank-simulator",
        data={"account_id": str(accounts["المجمّع"].id)},
        headers={"Accept": "application/json"},
    ).json()
    assert 500 <= random_amount["amount"] <= 15_000


def test_simulator_rejects_non_incoming_account(client, company, accounts):
    response = client.post(
        f"/entities/{company.id}/bank-simulator",
        data={"account_id": str(accounts["المؤجر"].id)},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 422


def post_webhook(client, payload, secret=SECRET):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/bank/incoming", content=body, headers={"X-Signature": sign(body, secret), "Content-Type": "application/json"}
    )


def test_bank_webhook(client, db, company, accounts, monkeypatch):
    activate_policy(client, db, company, accounts)
    client.post("/logout")  # الإشعار يجي من البنك بدون جلسة دخول
    payload = {"iban": POS, "amount": "4000.00", "occurred_at": "2026-09-02T10:00:00+03:00", "reference": "NEO-1"}

    monkeypatch.delenv("BANK_WEBHOOK_SECRET", raising=False)
    assert post_webhook(client, payload).status_code == 503  # الربط غير مفعّل

    monkeypatch.setenv("BANK_WEBHOOK_SECRET", SECRET)
    assert post_webhook(client, payload, secret="wrong").status_code == 401
    first = post_webhook(client, payload)
    assert first.status_code == 200 and first.json()["status"] == "allocated"
    again = post_webhook(client, payload)  # نفس الإشعار مرة ثانية
    assert again.json()["transaction_id"] == first.json()["transaction_id"]
    assert db.query(models.Transaction).count() == 1

    unknown = post_webhook(client, payload | {"iban": LANDLORD, "reference": "NEO-2"})
    assert unknown.status_code == 422  # حساب خارجي، مو مصدر إيراد
    assert post_webhook(client, {"iban": POS}).status_code == 400


def _draft_form(**overrides):
    return {
        "bucket_id": [""], "name": ["الضريبة"], "priority": ["1"], "calc_type": ["percentage"], "value": ["15"],
        "frequency": ["immediate"], "settlement_day": [""], "protected": ["0"], "notes": "", "justification": "",
        "intent": "submit",
    } | overrides


def test_policy_needs_destination_iban(client, db, company):
    client.post(f"/entities/{company.id}/policy/draft")
    draft = pol.open_policy(db, company.id)
    client.post(f"/policies/{draft.id}/edit", data=_draft_form(destination=[""]))
    db.refresh(draft)
    assert draft.status is models.PolicyStatus.DRAFT
    assert "ما له آيبان وجهة" in client.get(f"/policies/{draft.id}/edit").text


def test_new_iban_from_policy_creates_account(client, db, company):
    client.post(f"/entities/{company.id}/policy/draft")
    draft = pol.open_policy(db, company.id)
    client.post(
        f"/policies/{draft.id}/edit",
        data=_draft_form(destination=["new"], dest_iban=["sa11 1000 0000 0000 0000 0001"], dest_kind=["sub"]),
    )
    db.refresh(draft)
    assert draft.status is models.PolicyStatus.ACTIVE  # اعتماد ذاتي
    account = db.query(models.BankAccount).one()
    assert (account.iban, account.kind, account.name) == (TAX, AccountKind.SUB, "الضريبة")
    assert draft.buckets[0].destination_account_id == account.id


def test_source_iban_cannot_be_destination(client, db, company, accounts):
    client.post(f"/entities/{company.id}/policy/draft")
    draft = pol.open_policy(db, company.id)
    response = client.post(
        f"/policies/{draft.id}/edit", data=_draft_form(destination=["new"], dest_iban=[POS], dest_kind=["sub"])
    )
    assert response.status_code == 422 and "مصدر إيراد" in response.text


def test_registration_goes_to_accounts_with_pool_from_iban(client, db, make_user):
    make_user("acc2")
    client.post("/login", data={"username": "acc2", "password": "password123"})
    response = client.post(
        "/companies/new",
        data={"name": "شركة جديدة", "approval_mode": "self", "bank_name": "الراجحي", "iban": POOL},
        follow_redirects=False,
    )
    entity = db.query(models.Entity).filter_by(name="شركة جديدة").one()
    assert response.headers["location"] == f"/entities/{entity.id}/accounts"
    assert acc.pool_account(db, entity.id).iban == POOL


def test_semiannual_item_needs_and_saves_due_date(client, db, company):
    client.post(f"/entities/{company.id}/policy/draft")
    draft = pol.open_policy(db, company.id)
    form = _draft_form(
        name=["الإيجار"], calc_type=["fixed_amount"], value=["60000"], frequency=["semiannual"],
        destination=["new"], dest_iban=[LANDLORD], dest_kind=["external"],
    )
    client.post(f"/policies/{draft.id}/edit", data=form | {"due_date": [""]})
    db.refresh(draft)
    assert draft.status is models.PolicyStatus.DRAFT  # بدون تاريخ استحقاق ما تنعتمد
    assert "تاريخ الاستحقاق القادم" in client.get(f"/policies/{draft.id}/edit").text

    client.post(f"/policies/{draft.id}/edit", data=form | {"due_date": ["2027-03-01"]})
    db.refresh(draft)
    assert draft.status is models.PolicyStatus.ACTIVE
    assert str(draft.buckets[0].due_date) == "2027-03-01"
