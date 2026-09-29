# مفتاح الخزينة

نظام Profit First قابل للتخصيص. الخطة والقرارات في [PLAN.md](PLAN.md).

## التشغيل محليًا

```bash
py -3.12 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/alembic upgrade head
.venv/Scripts/python -m app.cli create-admin --username admin --full-name "مدير النظام"
.venv/Scripts/uvicorn app.main:app --reload
```

ثم افتح http://localhost:8000

للتجربة السريعة (محليًا فقط): `python -m app.cli seed-demo` ينشئ حسابات تجريبية وشركة بسياسة معتمدة ومعاملات. أسماء الحسابات وكلمات مرورها في `app/cli.py`.

## الاختبارات

```bash
.venv/Scripts/python -m pytest -q
```

## الهيكل

- `app/domain/` — محرك التخصيص والقواعد، بدون أي اعتماد على قاعدة البيانات
- `app/models.py` — الجداول
- `app/main.py` — الواجهة
- `migrations/` — تغييرات قاعدة البيانات (Alembic)
