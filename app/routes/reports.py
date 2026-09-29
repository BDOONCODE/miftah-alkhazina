"""التقارير وسجل التدقيق."""
from __future__ import annotations

from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user, get_entity_for
from ..db import get_session
from ..models import AuditLog, User
from ..services import metrics as m
from ..services import reports as svc
from ..templating import templates

router = APIRouter()


def _params(kind: str | None, anchor: str | None) -> tuple[str, date]:
    kind = kind if kind in m.REPORT_KINDS else "month"
    try:
        day = date.fromisoformat(anchor) if anchor else m.today_riyadh()
    except ValueError:
        day = m.today_riyadh()
    return kind, day


@router.get("/entities/{entity_id}/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    entity_id: int,
    kind: str | None = None,
    anchor: str | None = None,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    entity = get_entity_for(user, entity_id, session)
    kind, day = _params(kind, anchor)
    return templates.TemplateResponse(
        request,
        "reports/page.html",
        {
            "user": user,
            "entity": entity,
            "tab": "reports",
            "report": svc.build_report(session, entity, kind, day),
            "kind": kind,
            "anchor": day.isoformat(),
            "KINDS": m.REPORT_KINDS,
        },
    )


@router.get("/entities/{entity_id}/reports.xlsx")
def download_report(
    request: Request,
    entity_id: int,
    kind: str | None = None,
    anchor: str | None = None,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
):
    entity = get_entity_for(user, entity_id, session)
    kind, day = _params(kind, anchor)
    report = svc.build_report(session, entity, kind, day)
    session.add(AuditLog(user_id=user.id, entity_id=entity.id, action="report.downloaded", details={"kind": kind, "anchor": day.isoformat()}))
    session.commit()
    filename = f"{entity.name} - {report.title}.xlsx".replace("/", "-")
    return Response(
        svc.to_excel(report),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=\"report.xlsx\"; filename*=UTF-8''{quote(filename)}"},
    )


ACTION_LABELS = {
    "company.registered": "تسجيل الشركة",
    "company.updated": "تعديل بيانات الشركة",
    "company.reviewer_added": "إضافة مراجع",
    "company.approval_mode_changed": "تغيير طريقة الاعتماد",
    "policy.draft_created": "إنشاء مسودة سياسة",
    "policy.saved": "حفظ مسودة",
    "policy.submitted": "إرسال للاعتماد",
    "policy.approved": "اعتماد سياسة",
    "policy.rejected": "رفض سياسة",
    "policy.draft_discarded": "حذف مسودة",
    "policy.withdrawn": "سحب السياسة للتعديل",
    "transaction.recorded": "تسجيل معاملة",
    "transaction.reversed": "قيد عكسي",
    "dashboard.widget_added": "إضافة عنصر للوحة",
    "dashboard.widget_updated": "تعديل عنصر",
    "dashboard.widget_deleted": "حذف عنصر",
    "metric.saved": "حفظ مؤشر مخصص",
    "metric.deleted": "حذف مؤشر مخصص",
    "report.downloaded": "تنزيل تقرير",
}


@router.get("/entities/{entity_id}/audit", response_class=HTMLResponse)
def audit_page(request: Request, entity_id: int, user: User = Depends(current_user), session: Session = Depends(get_session)):
    entity = get_entity_for(user, entity_id, session)
    entries = session.scalars(
        select(AuditLog).where(AuditLog.entity_id == entity_id).order_by(AuditLog.id.desc()).limit(300)
    ).all()
    return templates.TemplateResponse(
        request,
        "reports/audit.html",
        {"user": user, "entity": entity, "tab": "audit", "entries": entries, "LABELS": ACTION_LABELS},
    )
