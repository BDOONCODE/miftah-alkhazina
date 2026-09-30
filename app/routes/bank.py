"""استقبال إشعارات البنك (Webhook) — نقطة الربط مع مزوّد المصرفية المفتوحة (نيوتك).

الشكل المتوقع (بيتعدّل حسب توثيق نيوتك لما يوصل):
    POST /api/bank/incoming
    X-Signature: hex(HMAC-SHA256(BANK_WEBHOOK_SECRET, جسم الطلب))
    {"iban": "SA..", "amount": "1500.00", "occurred_at": "2026-09-30T10:00:00+03:00", "reference": "..."}
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..db import get_session
from ..domain.money import parse_amount
from ..domain.periods import RIYADH
from ..models import utcnow
from ..services.transactions import TransactionError, ingest_bank_credit

router = APIRouter()


def _secret() -> str:
    return os.environ.get("BANK_WEBHOOK_SECRET", "").strip()


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@router.post("/api/bank/incoming", include_in_schema=False)
async def bank_incoming(request: Request, session: Session = Depends(get_session)):
    secret = _secret()
    if not secret:
        return JSONResponse({"error": "الربط البنكي غير مفعّل"}, status_code=503)
    body = await request.body()
    if not hmac.compare_digest(sign(body, secret), request.headers.get("X-Signature", "")):
        return JSONResponse({"error": "توقيع غير صحيح"}, status_code=401)
    try:
        data = json.loads(body)
        amount = parse_amount(str(data["amount"]))
        occurred_at = datetime.fromisoformat(data["occurred_at"]) if data.get("occurred_at") else utcnow()
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=RIYADH)
        txn = ingest_bank_credit(session, str(data["iban"]), amount, occurred_at, str(data["reference"]))
    except TransactionError as exc:  # قبل ValueError لأنه نوع منه
        session.rollback()
        return JSONResponse({"error": str(exc)}, status_code=422)
    except (KeyError, ValueError, TypeError) as exc:
        session.rollback()
        return JSONResponse({"error": f"بيانات غير صالحة: {exc}"}, status_code=400)
    session.commit()
    return JSONResponse({"status": "allocated", "transaction_id": txn.id, "surplus": txn.surplus / 100})
