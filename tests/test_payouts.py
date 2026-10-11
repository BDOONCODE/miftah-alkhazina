"""تحويل مبالغ البنود للمستفيدين حسب الاستحقاق: فوري مع كل دخل، أو كامل يوم الاستحقاق. والفائض لحسابه."""
from datetime import date, datetime, timezone

import pytest

from app import models
from app.models import CalcType, Frequency, PayoutStatus
from app.services import accounts as acc
from app.services import payouts as po
from app.services import policies as pol
from app.services import transactions as tx

SAR = 100
ROYALTY_IBAN = "SA5550000000000000000005"
LANDLORD_IBAN = "SA6660000000000000000006"
RESERVE_IBAN = "SA7770000000000000000007"
OWNER_IBAN = "SA8880000000000000000008"


@pytest.fixture
def setup(db, make_user, monkeypatch):
    # «اليوم» ثابت في منتصف سبتمبر، عشان صرف الاستحقاق ما يعتمد على تاريخ تشغيل الاختبار
    monkeypatch.setattr(tx, "utcnow", lambda: datetime(2026, 9, 15, 9, tzinfo=timezone.utc))
    user = make_user("acc")
    entity = models.Entity(name="مزاج", approval_mode="self")
    db.add(entity)
    db.flush()
    user.entities.append(entity)
    add = lambda name, iban, kind: acc.add_account(db, entity, user, name=name, bank_name="", iban=iban, kind=kind)  # noqa: E731
    accounts = {
        "royalty": add("مانح الامتياز", ROYALTY_IBAN, "external"),
        "landlord": add("المؤجر", LANDLORD_IBAN, "external"),
        "reserve": add("حساب الاحتياطي", RESERVE_IBAN, "sub"),
        "owner": add("حساب المالك", OWNER_IBAN, "sub"),
    }
    draft = pol.start_draft(db, entity, user)
    pol.save_draft(db, draft, [
        pol.BucketInput(None, "الإتاوة", 1, CalcType.PERCENTAGE, 600, Frequency.IMMEDIATE, None, "", False,
                        destination_account_id=accounts["royalty"].id),
        pol.BucketInput(None, "الإيجار", 2, CalcType.FIXED_AMOUNT, 3_000 * SAR, Frequency.MONTHLY, None, "", False,
                        destination_account_id=accounts["landlord"].id),
        pol.BucketInput(None, "الاحتياطي", 3, CalcType.PERCENTAGE, 1000, Frequency.MONTHLY, None, "", False,
                        destination_account_id=accounts["reserve"].id),
    ], "", user, surplus_account_id=accounts["owner"].id)
    pol.submit(db, draft, user, "")
    pol.approve(db, draft, user)
    db.commit()
    ids = {pb.name: pb.bucket_id for pb in draft.buckets}
    return entity, user, ids, accounts


def deposit(db, entity, user, amount, day):
    txn = tx.record_transaction(db, entity, user, amount, datetime(2026, 9, day, 10, tzinfo=timezone.utc))
    db.commit()
    return txn


def test_approval_opens_a_virtual_account_per_item_in_the_background(db, setup):
    buckets = db.query(models.Bucket).all()
    assert len(buckets) == 3
    assert all(acc.IBAN_RE.match(b.virtual_iban) and b.virtual_ref for b in buckets)
    assert len({b.virtual_iban for b in buckets}) == 3


def test_undated_item_and_surplus_transfer_with_each_deposit(db, setup):
    entity, user, ids, accounts = setup
    txn = deposit(db, entity, user, 10_000 * SAR, 10)
    paid = {(p.bucket_id, p.reason): p for p in db.query(models.Payout)}
    royalty = paid[(ids["الإتاوة"], "immediate")]
    assert (royalty.amount, royalty.beneficiary_account_id, royalty.transaction_id) == (600 * SAR, accounts["royalty"].id, txn.id)
    surplus = paid[(None, "surplus")]
    assert (surplus.amount, surplus.beneficiary_account_id) == (txn.surplus, accounts["owner"].id)
    assert len(paid) == 2  # الإيجار والاحتياطي لهم موعد: محجوزين
    balances = po.balances(db, entity.id)
    assert balances[ids["الإتاوة"]] == 0
    assert balances[ids["الاحتياطي"]] == 1_000 * SAR


def test_surplus_stays_in_pool_by_default(db, setup):
    entity, user, *_ = setup
    pol.active_policy(db, entity.id).surplus_account_id = None
    deposit(db, entity, user, 10_000 * SAR, 10)
    assert not db.query(models.Payout).filter_by(reason="surplus").count()


def test_dated_item_accumulates_then_pays_on_due_date_once(db, setup):
    entity, user, ids, accounts = setup
    deposit(db, entity, user, 10_000 * SAR, 10)
    deposit(db, entity, user, 10_000 * SAR, 30)  # آخر يوم بالدورة: يكتمل الهدف
    rent = ids["الإيجار"]
    assert po.balances(db, entity.id)[rent] == 3_000 * SAR
    assert po.run_due(db, entity.id, date(2026, 9, 30)) == []  # قبل الموعد ما يتحرك

    paid = {p.bucket_id: p for p in po.run_due(db, entity.id, date(2026, 10, 1))}
    assert (paid[rent].amount, paid[rent].beneficiary_account_id, paid[rent].period_key) == (
        3_000 * SAR, accounts["landlord"].id, "2026-09-01"
    )
    assert paid[ids["الاحتياطي"]].amount == 2_000 * SAR
    assert po.run_due(db, entity.id, date(2026, 10, 3)) == []  # ما ينصرف مرتين
    assert po.balances(db, entity.id)[rent] == 0


def test_due_payout_leaves_new_period_money(db, setup, monkeypatch):
    entity, user, ids, _ = setup
    deposit(db, entity, user, 10_000 * SAR, 20)
    # دخل يوم ٢ أكتوبر قبل ما يشتغل الصرف: نصيب أكتوبر يبقى، وسبتمبر بس ينصرف
    monkeypatch.setattr(tx, "utcnow", lambda: datetime(2026, 9, 30, 9, tzinfo=timezone.utc))
    tx.record_transaction(db, entity, user, 10_000 * SAR, datetime(2026, 10, 2, 10, tzinfo=timezone.utc))
    rent = ids["الإيجار"]
    october = po._allocated_in_period(db, rent, "2026-10-01")
    assert october > 0
    paid = {p.bucket_id: p for p in po.run_due(db, entity.id, date(2026, 10, 2))}
    assert paid[rent].amount == po._allocated_in_period(db, rent, "2026-09-01")
    assert po.balances(db, entity.id)[rent] == october


def test_reversal_blocked_after_money_left(db, setup):
    entity, user, *_ = setup
    txn = deposit(db, entity, user, 10_000 * SAR, 10)
    with pytest.raises(tx.TransactionError, match="تحوّل للمستفيدين"):
        tx.reverse_transaction(db, txn, user, "غلط")


def test_failed_transfer_keeps_money_held(db, setup, monkeypatch):
    from app.services import banking

    def refuse(**_):
        raise banking.BankingError("البنك ما رد")

    monkeypatch.setattr(banking._provider, "transfer", refuse)
    entity, user, ids, _ = setup
    deposit(db, entity, user, 10_000 * SAR, 10)
    assert {p.status for p in db.query(models.Payout)} == {PayoutStatus.FAILED}
    assert po.balances(db, entity.id)[ids["الإتاوة"]] == 600 * SAR  # ما طلع، فيبقى محجوز


def test_balances_page_hides_virtual_accounts(client, db, setup):
    entity, user, *_ = setup
    deposit(db, entity, user, 10_000 * SAR, 10)
    client.post("/login", data={"username": "acc", "password": "password123"})
    page = client.get(f"/entities/{entity.id}/balances").text
    for text in ("أرصدة البنود", "الإتاوة", "مانح الامتياز", "فور التقسيم", "الفائض", "حساب المالك", "وضع المحاكاة"):
        assert text in page
    assert "SA90" not in page and "افتراضي" not in page
