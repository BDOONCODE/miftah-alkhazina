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
            username=username, full_name=username, password_hash=hash_password(password), role=role
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
