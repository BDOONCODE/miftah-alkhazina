from fastapi.templating import Jinja2Templates

from .config import BASE_DIR
from .domain.money import format_amount, format_percent
from .domain.periods import RIYADH
from .web import pop_flashes

templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
templates.env.filters["sar"] = format_amount
templates.env.filters["pct"] = format_percent
templates.env.filters["riyadh"] = lambda dt, fmt="%Y-%m-%d %H:%M": dt.astimezone(RIYADH).strftime(fmt) if dt else ""
templates.env.globals["pop_flashes"] = pop_flashes

templates.env.globals.update(
    ROLE_LABELS={"admin": "مدير", "accountant": "محاسب", "approver": "معتمِد"},
    CALC_LABELS={"percentage": "نسبة", "fixed_amount": "مبلغ ثابت"},
    FREQ_LABELS={
        "immediate": "فوري",
        "daily": "يومي",
        "weekly": "أسبوعي",
        "monthly": "شهري",
        "day_of_month": "يوم محدد بالشهر",
    },
    POLICY_STATUS_LABELS={
        "draft": "مسودة",
        "pending": "بانتظار الاعتماد",
        "active": "نشطة",
        "archived": "مؤرشفة",
        "rejected": "مرفوضة",
    },
    FUNDING_LABELS={
        "FULLY_FUNDED": "ممول بالكامل",
        "PARTIAL": "ممول جزئيًا",
        "UNFUNDED": "غير ممول",
        "NOT_REQUIRED": "غير مطلوب",
    },
)
