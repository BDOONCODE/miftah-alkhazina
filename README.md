# Miftah Alkhazina (مفتاح الخزينة)

A configurable Profit First system: every incoming amount is split immediately according to a policy the accountant defines per company. Plan and decisions are in [PLAN.md](PLAN.md); deployment steps are in [DEPLOY.md](DEPLOY.md).

The user interface is in Arabic; code, comments and documentation are in English.

## Running locally

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/alembic upgrade head
.venv/Scripts/python -m app.cli create-admin --username admin --full-name "System Admin"
.venv/Scripts/uvicorn app.main:app --reload
```

Then open http://localhost:8000

For a quick demo (local only): `python -m app.cli seed-demo` creates demo accounts and a company with an approved policy and transactions. The account names and passwords are in `app/cli.py`.

## Tests

```bash
.venv/Scripts/python -m pytest -q
```

## Layout

- `app/domain/` — the allocation engine and rules, with no database dependency
- `app/services/` — business logic (policies, transactions, payouts, metrics, reports)
- `app/services/banking.py` — the banking provider layer (simulated until the Neotek integration)
- `app/routes/` and `app/templates/` — the web UI
- `app/models.py` — database tables
- `migrations/` — database migrations (Alembic)
- `docs/` — the illustrated overview for stakeholders (in Arabic)
