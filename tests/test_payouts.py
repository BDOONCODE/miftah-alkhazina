"""Transferring item balances to beneficiaries by schedule: immediately with each deposit, or in full on the due date. And the surplus to its account."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app import models
from app.models import CalcType, Frequency, PayoutStatus
from app.services import accounts as acc
from app.services import payouts as po
from app.services import policies as pol
from app.services import transactions as tx
from app.services import virtual_ibans as vib

SAR = 100
ROYALTY_IBAN = "SA5550000000000000000005"
LANDLORD_IBAN = "SA6660000000000000000006"
RESERVE_IBAN = "SA7770000000000000000007"
OWNER_IBAN = "SA8880000000000000000008"
OWN_IBAN = "SA9990000000000000000009"


@pytest.fixture
def setup(db, make_user, monkeypatch):
    # "Today" is fixed mid-September, so due payouts do not depend on when the test runs
    monkeypatch.setattr(tx, "utcnow", lambda: datetime(2026, 9, 15, 9, tzinfo=timezone.utc))
    user = make_user("acc")
    entity = models.Entity(name="مزاج", approval_mode="self")
    db.add(entity)
    db.flush()
    user.entities.append(entity)
    add = lambda name, iban, kind: acc.add_account(db, entity, user, name=name, bank_name="", iban=iban, kind=kind)  # noqa: E731
    accounts = {
        "own": add("الحساب الجاري", OWN_IBAN, "source"),
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


def test_approval_gives_the_company_its_deposit_iban(db, setup):
    entity, *_ = setup
    deposit_iban = vib.for_entity(db, entity.id)
    assert deposit_iban is not None and acc.IBAN_RE.match(deposit_iban.iban)
    assert vib.entity_for(db, deposit_iban.iban).id == entity.id
    assert all(b.virtual_iban is None for b in db.query(models.Bucket))  # items are ledger balances, not bank accounts


def test_undated_item_and_surplus_transfer_with_each_deposit(db, setup):
    entity, user, ids, accounts = setup
    txn = deposit(db, entity, user, 10_000 * SAR, 10)
    paid = {(p.bucket_id, p.reason): p for p in db.query(models.Payout)}
    royalty = paid[(ids["الإتاوة"], "immediate")]
    assert (royalty.amount, royalty.beneficiary_account_id, royalty.transaction_id) == (600 * SAR, accounts["royalty"].id, txn.id)
    assert len(paid) == 1  # rent and reserve have a schedule: held; the surplus stays in the pool this month
    # "today" is the 15th: rent is behind its schedule (deposit on the 10th), so the pool's surplus covers it
    covered = sum(m.amount for m in db.query(models.PoolMove).filter_by(kind="cover"))
    assert covered == 500 * SAR  # rent: 3,000 x 15/30 = 1,500 due by the 15th, 1,000 came from the split
    assert po.pool_balance(db, entity.id) == txn.surplus - covered
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
    deposit(db, entity, user, 10_000 * SAR, 30)  # last day of the cycle: the target completes
    rent = ids["الإيجار"]
    assert po.balances(db, entity.id)[rent] == 3_000 * SAR
    assert po.run_due(db, entity.id, date(2026, 9, 30)) == []  # nothing moves before the due date

    paid = {p.bucket_id: p for p in po.run_due(db, entity.id, date(2026, 10, 1))}
    assert (paid[rent].amount, paid[rent].beneficiary_account_id, paid[rent].period_key) == (
        3_000 * SAR, accounts["landlord"].id, "2026-09-01"
    )
    assert paid[ids["الاحتياطي"]].amount == 2_000 * SAR
    assert po.run_due(db, entity.id, date(2026, 10, 3)) == []  # never paid twice
    assert po.balances(db, entity.id)[rent] == 0


def test_due_payout_leaves_new_period_money(db, setup, monkeypatch):
    entity, user, ids, _ = setup
    deposit(db, entity, user, 10_000 * SAR, 20)
    # Income on October 2 before payouts ran: October's share stays, only September is paid
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
    with pytest.raises(tx.TransactionError, match="حُوِّل جزء من هذه المعاملة"):
        tx.reverse_transaction(db, txn, user, "غلط")


def test_failed_transfer_keeps_money_held(db, setup, monkeypatch):
    from app.services import banking

    def refuse(**_):
        raise banking.BankingError("البنك ما رد")

    monkeypatch.setattr(banking._provider, "transfer", refuse)
    entity, user, ids, _ = setup
    deposit(db, entity, user, 10_000 * SAR, 10)
    assert {p.status for p in db.query(models.Payout)} == {PayoutStatus.FAILED}
    assert po.balances(db, entity.id)[ids["الإتاوة"]] == 600 * SAR  # it did not leave, so it stays held


def test_balances_page_hides_virtual_accounts(client, db, setup):
    entity, user, *_ = setup
    deposit(db, entity, user, 10_000 * SAR, 10)
    client.post("/login", data={"username": "acc", "password": "password123"})
    page = client.get(f"/entities/{entity.id}/balances").text
    for text in ("أرصدة البنود", "الإتاوة", "مانح الامتياز", "فور التقسيم", "الفائض", "حساب المالك", "وضع المحاكاة"):
        assert text in page
    assert "SA90" not in page and "افتراضي" not in page


class RecordingProvider:
    """Records every provider call in order, on top of the simulation."""

    def __init__(self):
        from app.services.banking import SimulatedProvider

        self.sim, self.calls, self.sent = SimulatedProvider(), [], []

    def __getattr__(self, name):
        method = getattr(self.sim, name)

        def call(*args, **kwargs):
            self.calls.append((name, kwargs.get("amount")))
            if name == "transfer":
                self.sent.append(kwargs)
            return method(*args, **kwargs)

        return call


def test_only_payouts_reach_the_bank(db, setup, monkeypatch):
    """The split lives in the ledger: the one call to the bank is the payout, from the master, naming the client."""
    from app.services import banking

    recorder = RecordingProvider()
    monkeypatch.setattr(banking, "_provider", recorder)
    entity, user, ids, _ = setup
    deposit(db, entity, user, 10_000 * SAR, 10)
    assert [c for c in recorder.calls if c[0] == "transfer"] == [("transfer", 600 * SAR)]  # the royalty, immediate
    payout = db.query(models.Payout).one()
    assert recorder.sent[0]["from_ref"] == "sim-master"
    assert recorder.sent[0]["description"] == f"نيابة عن مزاج: الإتاوة {payout.period_key}"


def test_pool_surplus_covers_deficits_nearest_due_first(db, setup, monkeypatch):
    """A big deposit leaves surplus in the pool; a later small one leaves items short, and the pool covers
    exactly what is behind schedule: rent (due at month end) before the reserve (same date, lower priority)."""
    entity, user, ids, _ = setup
    today = lambda day: monkeypatch.setattr(tx, "utcnow", lambda: datetime(2026, 9, day, 12, tzinfo=timezone.utc))  # noqa: E731
    today(10)
    deposit(db, entity, user, 10_000 * SAR, 10)  # rent gets 1,000 (10/30 of 3,000)
    pool = po.pool_balance(db, entity.id)
    today(20)
    small = deposit(db, entity, user, 100 * SAR, 20)  # royalty 6, rent 94 of the 1,000 due, reserve nothing
    covers = {m.bucket_id: m.amount for m in db.query(models.PoolMove).filter_by(kind="cover")}
    assert covers == {ids["الإيجار"]: 906 * SAR, ids["الاحتياطي"]: 10 * SAR}
    states = {s.bucket_id: s for s in db.query(models.BucketPeriodState)}
    assert states[ids["الإيجار"]].funded == 2_000 * SAR  # back on schedule: 3,000 x 20/30
    assert states[ids["الاحتياطي"]].carried_deficit == 0
    assert po.pool_balance(db, entity.id) == pool - 916 * SAR
    assert all(m.transaction_id == small.id for m in db.query(models.PoolMove))


def test_cover_order_prefers_nearest_due_date(db, setup):
    entity, user, ids, _ = setup
    policy = pol.active_policy(db, entity.id)
    order = [pb.name for pb in sorted(policy.buckets, key=lambda b: (po._due_on(b, date(2026, 9, 12)), b.priority))]
    assert order == ["الإتاوة", "الإيجار", "الاحتياطي"]  # undated first; same due date -> priority


def test_month_start_pays_last_months_surplus_to_the_chosen_account(db, setup):
    entity, user, ids, accounts = setup
    deposit(db, entity, user, 10_000 * SAR, 30)
    surplus = po.pool_balance(db, entity.id)
    assert surplus > 0
    actions = po.run_due(db, entity.id, date(2026, 10, 1))
    sent = [p for p in actions if isinstance(p, models.Payout) and p.reason == "surplus"]
    assert [(p.amount, p.beneficiary_account_id, p.period_key) for p in sent] == [(surplus, accounts["owner"].id, "2026-09-01")]
    assert po.surplus_balance(db, entity.id) == 0
    assert not db.query(models.PoolMove).filter_by(kind="isolate").count()  # no separate surplus account any more
    assert po.run_due(db, entity.id, date(2026, 10, 2)) == []  # once a month


def test_surplus_stays_in_the_balance_without_a_destination(db, setup):
    entity, user, *_ = setup
    pol.active_policy(db, entity.id).surplus_account_id = None
    deposit(db, entity, user, 10_000 * SAR, 30)
    surplus = po.pool_balance(db, entity.id)
    po.run_due(db, entity.id, date(2026, 10, 1))
    assert po.surplus_balance(db, entity.id) == surplus


def test_reversal_blocked_after_pool_was_used(db, setup):
    entity, user, *_ = setup
    pol.active_policy(db, entity.id).surplus_account_id = None
    deposit(db, entity, user, 10_000 * SAR, 10)
    small = deposit(db, entity, user, 100 * SAR, 12)
    if not db.query(models.Payout).filter_by(transaction_id=small.id).count():
        with pytest.raises(tx.TransactionError, match="الفائض"):
            tx.reverse_transaction(db, small, user, "غلط")



def test_surplus_is_paid_on_a_chosen_day_of_month(db, setup):
    entity, user, ids, accounts = setup
    policy = pol.active_policy(db, entity.id)
    policy.surplus_frequency, policy.surplus_settlement_day = Frequency.DAY_OF_MONTH, 25
    deposit(db, entity, user, 10_000 * SAR, 20)
    surplus = po.pool_balance(db, entity.id)
    assert po.pay_surplus(db, entity.id, policy, date(2026, 9, 24)) == []  # not the 25th yet
    [payout] = po.pay_surplus(db, entity.id, policy, date(2026, 9, 25))
    assert (payout.amount, payout.period_key) == (surplus, "2026-08-25") and po.surplus_balance(db, entity.id) == 0


def test_surplus_schedule_needs_its_day(db, setup):
    entity, user, *_ = setup
    draft = pol.start_draft(db, entity, user)
    draft.surplus_frequency, draft.surplus_settlement_day = Frequency.DAY_OF_MONTH, None
    assert any("يوم تحويل الفائض" in e for e in pol.destination_errors(draft))



def _login(client, username):
    client.post("/logout")
    client.post("/login", data={"username": username, "password": "password123"})


def test_failed_transfer_reaches_support_without_amount(client, db, setup, make_user, monkeypatch):
    from app.services import banking

    def refuse(**_):
        raise banking.BankingError("البنك ما رد")

    monkeypatch.setattr(banking._provider, "transfer", refuse)
    entity, user, *_ = setup
    deposit(db, entity, user, 10_000 * SAR, 10)
    issue = db.query(models.TransferIssue).one()
    assert (issue.leg, issue.destination, issue.resolved) == ("payout", "مانح الامتياز", False)

    make_user("boss", role=models.Role.ADMIN)
    _login(client, "boss")
    page = client.get("/admin/support").text
    assert "مزاج" in page and "مانح الامتياز" in page and issue.reference in page and "← المستفيد" in page
    assert "600.00" not in page and ROYALTY_IBAN not in page and "10,000" not in page
    client.post(f"/admin/issues/{issue.id}/resolve", data={"note": "أعيد التحويل مع نيوتك"})
    db.refresh(issue)
    assert issue.resolved and issue.note == "أعيد التحويل مع نيوتك"


def test_deposit_from_unregistered_account_waits_for_the_accountant(client, db, setup):
    entity, user, *_ = setup
    deposit_iban = vib.for_entity(db, entity.id).iban
    held = tx.ingest_bank_credit(db, deposit_iban, "SA5500000000000000000099", 2_000 * SAR,
                                 datetime(2026, 9, 10, 10, tzinfo=timezone.utc), "NEO-77", sender_name="مؤسسة أخرى")
    db.commit()
    assert isinstance(held, models.HeldDeposit) and db.query(models.Transaction).count() == 0

    _login(client, "acc")
    page = client.get(f"/entities/{entity.id}/transactions").text
    assert "حوالات بانتظار تأكيدك" in page and "مؤسسة أخرى" in page
    client.post(f"/held-deposits/{held.id}/accept", data={"register": "1"})
    db.refresh(held)
    txn = db.get(models.Transaction, held.transaction_id)
    assert held.status == "accepted" and txn.amount == 2_000 * SAR and txn.external_ref == "NEO-77"
    assert db.get(models.BankAccount, txn.source_account_id).iban == "SA5500000000000000000099"  # registered now

    again = tx.ingest_bank_credit(db, deposit_iban, "SA5500000000000000000099", 500 * SAR,
                                  datetime(2026, 9, 11, 10, tzinfo=timezone.utc), "NEO-78")
    assert isinstance(again, models.Transaction)  # the next transfer from it is split directly


def test_refused_deposit_goes_to_support_for_return(client, db, setup):
    entity, *_ = setup
    held = tx.ingest_bank_credit(db, vib.for_entity(db, entity.id).iban, "SA5500000000000000000099", 900 * SAR,
                                 datetime(2026, 9, 10, 10, tzinfo=timezone.utc), "NEO-90", sender_name="مجهول")
    db.commit()
    _login(client, "acc")
    client.post(f"/held-deposits/{held.id}/reject")
    db.refresh(held)
    issue = db.query(models.TransferIssue).one()
    assert held.status == "rejected" and (issue.leg, issue.reference) == ("deposit", "NEO-90")


def test_surplus_transfer_now_only_to_own_accounts(db, setup):
    entity, user, ids, accounts = setup
    pol.active_policy(db, entity.id).surplus_account_id = None
    deposit(db, entity, user, 10_000 * SAR, 10)
    available = po.surplus_balance(db, entity.id)
    assert available > 0
    with pytest.raises(po.PayoutError, match="المسجّلة"):
        po.withdraw(db, entity, user, 100 * SAR, accounts["royalty"].id)
    with pytest.raises(po.PayoutError, match="أكبر من رصيد"):
        po.withdraw(db, entity, user, available + 1, accounts["own"].id)
    [payout] = po.withdraw(db, entity, user, 100 * SAR, accounts["own"].id)
    assert payout.reason == "withdrawal" and po.surplus_balance(db, entity.id) == available - 100 * SAR


def test_inventory_assigns_waiting_companies_and_freezes_released(db, make_user, monkeypatch):
    from app.services import banking

    class Live:  # not the simulation: no test IBANs are issued
        def master_ref(self):
            return "live-master"

        def transfer(self, **kwargs):
            return "live-tr"

    monkeypatch.setattr(banking, "_provider", Live())
    admin = make_user("boss", role=models.Role.ADMIN)
    entity = models.Entity(name="مزاج", approval_mode="self")
    db.add(entity)
    db.flush()
    assert vib.assign(db, entity) is None  # empty inventory: the company waits
    db.add(models.Policy(entity_id=entity.id, version=1, status=models.PolicyStatus.ACTIVE, created_by=admin.id))
    db.flush()
    assert [e.id for e in vib.waiting(db)] == [entity.id]
    added, rejected = vib.add_ibans(db, "SA1100000000000000000071\nbad\nSA1100000000000000000072", admin)
    assert (added, rejected) == (2, ["bad"])
    row = vib.for_entity(db, entity.id)
    assert row.iban == "SA1100000000000000000071" and vib.waiting(db) == []
    vib.release(db, row, admin)
    assert vib.stock(db) == {"available": 1, "assigned": 0, "frozen": 1}
    row.released_at = row.released_at - timedelta(days=vib.FREEZE_DAYS + 1)
    assert vib.stock(db)["available"] == 2  # the freeze is over


def test_reversal_request_goes_to_support_and_back(client, db, setup, make_user):
    entity, user, *_ = setup
    txn = deposit(db, entity, user, 10_000 * SAR, 10)
    _login(client, "acc")
    assert "طلب عكس عبر الدعم" in client.get(f"/transactions/{txn.id}").text
    client.post(f"/transactions/{txn.id}/reversal-request", data={"reason": "حُوِّل إلى مستفيد خاطئ"})
    client.post(f"/transactions/{txn.id}/reversal-request", data={"reason": "مرة ثانية"})
    row = db.query(models.ReversalRequest).one()  # one open request per transaction
    assert row.status == "open"

    make_user("boss", role=models.Role.ADMIN)
    _login(client, "boss")
    assert "حُوِّل إلى مستفيد خاطئ" in client.get("/admin/support").text
    client.post(f"/admin/reversal-requests/{row.id}", data={"status": "done", "note": "عُكس مع البنك"})
    db.refresh(row)
    assert row.status == "done"

    _login(client, "acc")
    page = client.get(f"/transactions/{txn.id}").text
    assert "عُكس مع البنك" in page and ">تم<" in page
