import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app import models
from app.db import Base, get_session, make_engine
from app.main import app
from app.security import hash_password


@pytest.fixture
def db(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def _override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    with factory() as session:
        yield session
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture
def client(db):
    return TestClient(app)


@pytest.fixture
def make_user(db):
    def _make(username="acc", role=models.Role.ACCOUNTANT, password="password123", entities=()):
        user = models.User(
            username=username, full_name=username, password_hash=hash_password(password, enforce_rules=False), role=role
        )
        user.entities.extend(entities)
        db.add(user)
        db.commit()
        return user

    return _make


@pytest.fixture(autouse=True)
def _clear_login_throttle():
    from app import throttle

    throttle._failures.clear()


VAT_IBAN = "SA9990000000000000000099"


def give_vat_account(db, entity, user):
    """ضريبة القيمة المضافة إلزامية وتحتاج حساب تتحوّل له قبل اعتماد أي سياسة."""
    from app.services import accounts

    account = accounts.add_account(db, entity, user, name="حساب الضريبة", bank_name="", iban=VAT_IBAN, kind="sub")
    entity.vat_account_id = account.id
    db.commit()
    return account
