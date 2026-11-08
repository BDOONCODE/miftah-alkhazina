"""Client data is encrypted at rest: the database never holds a readable IBAN, name or audit detail."""
from sqlalchemy import text

from app import crypto, models

IBAN = "SA0380000000608010167519"


def test_round_trip_with_a_fresh_nonce_each_time():
    a, b = crypto.encrypt(IBAN), crypto.encrypt(IBAN)
    assert a != b and a.startswith(crypto.PREFIX)
    assert crypto.decrypt(a) == crypto.decrypt(b) == IBAN


def test_values_written_before_encryption_still_read():
    assert crypto.decrypt("مطعم قديم") == "مطعم قديم"
    assert crypto.decrypt_json('{"a": 1}') == {"a": 1}


def test_blind_index_ignores_spaces_and_case():
    assert crypto.iban_index(IBAN) == crypto.iban_index("sa03 8000 0000 6080 1016 7519")
    assert crypto.iban_index(IBAN) != crypto.iban_index("SA0380000000608010167518")


def test_database_holds_only_ciphertext(db):
    entity = models.Entity(name="مطعم مزاج", cr_number="1010101010", iban=IBAN)
    db.add(entity)
    db.flush()
    account = models.BankAccount(entity_id=entity.id, name="نقاط البيع", iban=IBAN, kind=models.AccountKind.SOURCE)
    db.add_all([account, models.AuditLog(entity_id=entity.id, action="test", details={"to": "المؤجر"})])
    db.commit()

    raw = db.execute(text("SELECT name, iban, iban_hash FROM bank_accounts")).one()
    assert raw.iban.startswith(crypto.PREFIX) and IBAN not in raw.iban
    assert "نقاط" not in raw.name
    assert raw.iban_hash == crypto.iban_index(IBAN)
    company = db.execute(text("SELECT name, cr_number, iban FROM entities")).one()
    assert all(v.startswith(crypto.PREFIX) for v in company)
    assert "المؤجر" not in db.execute(text("SELECT details FROM audit_log")).scalar_one()

    db.expire_all()
    assert db.get(models.BankAccount, account.id).iban == IBAN
    assert db.get(models.Entity, entity.id).name == "مطعم مزاج"
