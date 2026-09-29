"""المسار الكامل: المحاسب يسجّل الشركة والمراجع ← يبني السياسة ← المراجع يعتمدها."""
import pytest

from app import models
from app.models import PolicyStatus, Role
from app.services import policies as svc


def login(client, username, password="password123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=False)


def register(client, **overrides):
    data = {
        "name": "مؤسسة النخيل",
        "cr_number": "1010101010",
        "iban": "SA03 8000 0000 6080 1016 7519",
        "reviewer_full_name": "سارة المالك",
        "reviewer_username": "sara",
        "reviewer_password": "temp-pass-1",
    } | overrides
    return client.post("/companies/new", data=data, follow_redirects=False)


POLICY_FORM = {
    "bucket_id": ["", "", ""],
    "name": ["الضريبة", "الإيجار", "الأرباح"],
    "priority": ["1", "2", "3"],
    "calc_type": ["percentage", "fixed_amount", "percentage"],
    "value": ["15", "10,000", "5"],
    "frequency": ["immediate", "monthly", "monthly"],
    "settlement_day": ["", "", ""],
    "destination": ["", "حساب الإيجار", ""],
    "protected": ["1", "0", "0"],
    "notes": "",
    "justification": "",
}


@pytest.fixture
def company(client, db, make_user):
    make_user("acc")
    login(client, "acc")
    response = register(client)
    assert response.status_code == 303
    return db.query(models.Entity).one()


def test_registration_creates_company_reviewer_and_dashboard(company, db):
    assert company.iban == "SA0380000000608010167519"
    reviewer = db.query(models.User).filter_by(username="sara").one()
    assert reviewer.role is Role.APPROVER and reviewer.must_change_password
    assert {u.username for u in company.users} == {"acc", "sara"}
    assert db.query(models.Dashboard).filter_by(entity_id=company.id).count() == 1


def test_registration_rejects_bad_iban_and_duplicate_username(client, company):
    assert register(client, iban="12345", reviewer_username="x1").status_code == 422
    assert register(client, reviewer_username="sara").status_code == 422


def test_full_flow_accountant_builds_reviewer_approves(client, company, db):
    client.post(f"/entities/{company.id}/policy/draft")
    draft = svc.open_policy(db, company.id)
    response = client.post(f"/policies/{draft.id}/edit", data=POLICY_FORM | {"intent": "submit"}, follow_redirects=False)
    assert response.status_code == 303
    db.refresh(draft)
    assert draft.status is PolicyStatus.PENDING
    assert [(b.name, b.value) for b in draft.buckets] == [("الضريبة", 1500), ("الإيجار", 1_000_000), ("الأرباح", 500)]

    # المحاسب ما يقدر يعتمد
    assert client.post(f"/policies/{draft.id}/approve").status_code == 403

    # المراجع: أول دخول يجبره يغيّر كلمة المرور
    client.post("/logout")
    assert login(client, "sara", "temp-pass-1").headers["location"] == "/account/password"
    assert client.get("/", follow_redirects=False).headers["location"] == "/account/password"
    client.post("/account/password", data={"current": "temp-pass-1", "new": "sara-new-pass", "confirm": "sara-new-pass"})

    client.post(f"/policies/{draft.id}/approve", data={"note": "تمام"})
    db.refresh(draft)
    assert draft.status is PolicyStatus.ACTIVE
    assert draft.decider.username == "sara"


def test_invalid_policy_cannot_be_submitted(client, company, db):
    client.post(f"/entities/{company.id}/policy/draft")
    draft = svc.open_policy(db, company.id)
    bad = POLICY_FORM | {"value": ["60", "10000", "50"], "intent": "submit"}  # 110%
    client.post(f"/policies/{draft.id}/edit", data=bad)
    db.refresh(draft)
    assert draft.status is PolicyStatus.DRAFT


def test_unparseable_value_is_reported_without_saving(client, company, db):
    client.post(f"/entities/{company.id}/policy/draft")
    draft = svc.open_policy(db, company.id)
    response = client.post(f"/policies/{draft.id}/edit", data=POLICY_FORM | {"value": ["abc", "10000", "5"]})
    assert response.status_code == 422
    assert "السطر 1" in response.text
    db.refresh(draft)
    assert draft.buckets == []


def test_other_accountant_cannot_see_company(client, company, make_user):
    make_user("other")
    client.post("/logout")
    login(client, "other")
    assert client.get(f"/entities/{company.id}/policy").status_code == 404


def _activate(db, entity, accountant, reviewer):
    draft = svc.start_draft(db, entity, accountant)
    svc.save_draft(
        db,
        draft,
        [
            svc.BucketInput(None, "الضريبة", 1, models.CalcType.PERCENTAGE, 1500, models.Frequency.IMMEDIATE, None, "", True),
            svc.BucketInput(None, "الأرباح", 2, models.CalcType.PERCENTAGE, 500, models.Frequency.MONTHLY, None, "", False),
        ],
        "",
        accountant,
    )
    svc.submit(db, draft, accountant, "")
    svc.approve(db, draft, reviewer)
    db.commit()
    return draft


def test_four_eyes_and_protected_bucket_justification(db, company):
    accountant = db.query(models.User).filter_by(username="acc").one()
    reviewer = db.query(models.User).filter_by(username="sara").one()
    v1 = _activate(db, company, accountant, reviewer)
    tax_id = v1.buckets[0].bucket_id

    # الضريبة تموّلت هالفترة
    db.add(models.BucketPeriodState(bucket_id=tax_id, entity_id=company.id, period_key="0000-00-00", funded=100))
    db.commit()

    v2 = svc.start_draft(db, company, accountant)
    assert [b.bucket_id for b in v2.buckets] == [b.bucket_id for b in v1.buckets]  # نفس الهويات
    changed = [svc.BucketInput(tax_id, "الضريبة", 1, models.CalcType.PERCENTAGE, 1000, models.Frequency.IMMEDIATE, None, "", True)]
    svc.save_draft(db, v2, changed, "", accountant)

    with pytest.raises(svc.PolicyError, match="محمي"):
        svc.submit(db, v2, accountant, "قصير")
    svc.submit(db, v2, accountant, "تخفيض نسبة الضريبة بعد مراجعة الإقرار")

    with pytest.raises(svc.PolicyError, match="أنشأتها"):
        svc.approve(db, v2, _make_self_approver(db, accountant))

    svc.approve(db, v2, reviewer)
    db.commit()
    assert v1.status is PolicyStatus.ARCHIVED and v2.status is PolicyStatus.ACTIVE
    # البند المحذوف (الأرباح) ما عنده حالة، فما ينقفل شي؛ والضريبة باقية
    assert db.get(models.BucketPeriodState, tax_id) is not None


def _make_self_approver(db, accountant):
    """نفس الشخص حتى لو صار معتمِد: ما يعتمد اللي أنشأه."""
    accountant.role = Role.APPROVER
    return accountant


def test_removed_bucket_with_state_is_closed_on_approval(db, company):
    accountant = db.query(models.User).filter_by(username="acc").one()
    reviewer = db.query(models.User).filter_by(username="sara").one()
    v1 = _activate(db, company, accountant, reviewer)
    profit_id = v1.buckets[1].bucket_id
    db.add(
        models.BucketPeriodState(
            bucket_id=profit_id, entity_id=company.id, period_key="2026-09-01", funded=50, carried_deficit=30
        )
    )
    db.commit()

    v2 = svc.start_draft(db, company, accountant)
    svc.save_draft(db, v2, [svc.BucketInput(v1.buckets[0].bucket_id, "الضريبة", 1, models.CalcType.PERCENTAGE, 1500, models.Frequency.IMMEDIATE, None, "", True)], "", accountant)
    svc.submit(db, v2, accountant, "")
    svc.approve(db, v2, reviewer)
    db.commit()

    closure = db.query(models.PeriodClosure).one()
    assert (closure.bucket_id, closure.closed_deficit, closure.closed_by_policy_id) == (profit_id, 30, v2.id)
    assert db.get(models.BucketPeriodState, profit_id) is None


def test_withdraw_pending_and_edit_after_rejection(db, company):
    accountant = db.query(models.User).filter_by(username="acc").one()
    reviewer = db.query(models.User).filter_by(username="sara").one()
    tax = svc.BucketInput(None, "الضريبة", 1, models.CalcType.PERCENTAGE, 1500, models.Frequency.IMMEDIATE, None, "", True)

    draft = svc.start_draft(db, company, accountant)
    svc.save_draft(db, draft, [tax], "", accountant)
    svc.submit(db, draft, accountant, "")

    # سحب من المراجع للتعديل
    svc.withdraw(db, draft, accountant)
    assert draft.status is PolicyStatus.DRAFT and draft.submitted_at is None
    svc.submit(db, draft, accountant, "")

    # الرفض ثم مسودة جديدة تبدأ من البنود المرفوضة
    svc.reject(db, draft, reviewer, "النسبة عالية")
    db.commit()
    v2 = svc.start_draft(db, company, accountant)
    assert [(b.name, b.value, b.bucket_id) for b in v2.buckets] == [("الضريبة", 1500, draft.buckets[0].bucket_id)]
    assert v2.version == 2


def test_withdraw_via_page(client, company, db):
    client.post(f"/entities/{company.id}/policy/draft")
    draft = svc.open_policy(db, company.id)
    client.post(f"/policies/{draft.id}/edit", data=POLICY_FORM | {"intent": "submit"})
    assert "سحب للتعديل" in client.get(f"/entities/{company.id}/policy").text
    response = client.post(f"/policies/{draft.id}/withdraw", follow_redirects=False)
    assert response.headers["location"] == f"/policies/{draft.id}/edit"
    db.refresh(draft)
    assert draft.status is PolicyStatus.DRAFT
