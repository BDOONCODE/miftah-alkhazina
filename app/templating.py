import hashlib

from fastapi.templating import Jinja2Templates

from .config import BASE_DIR
from .domain.money import format_amount, format_percent
from .domain.periods import RIYADH
from .services.accounts import KIND_LABELS
from .services.payouts import REASON_LABELS
from .services.policies import APPROVAL_MODES
from .web import pop_flashes

templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
templates.env.filters["sar"] = format_amount
templates.env.filters["pct"] = format_percent
templates.env.filters["riyadh"] = lambda dt, fmt="%Y-%m-%d %H:%M": dt.astimezone(RIYADH).strftime(fmt) if dt else ""
templates.env.globals["pop_flashes"] = pop_flashes


def _asset_version() -> str:
    """رقم نسخة يتغيّر مع كل تحديث للملفات الثابتة، عشان المتصفح ياخذ النسخة الجديدة بدل المحفوظة عنده."""
    digest = hashlib.sha1()
    for path in sorted((BASE_DIR / "app" / "static").rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


templates.env.globals["ASSET_VERSION"] = _asset_version()
templates.env.globals["APPROVAL_MODES"] = APPROVAL_MODES
templates.env.globals["KIND_LABELS"] = KIND_LABELS
templates.env.globals.update(PAYOUT_REASONS=REASON_LABELS)

templates.env.globals.update(
    CHANNEL_LABELS={"manual": "يدوي", "open_banking": "تلقائي من البنك"},
    ROLE_LABELS={"admin": "مدير", "accountant": "محاسب", "approver": "معتمِد"},
    CALC_LABELS={"percentage": "نسبة", "fixed_amount": "مبلغ ثابت"},
    FREQ_LABELS={
        "immediate": "فوري (بدون موعد)",
        "daily": "يومي",
        "weekly": "أسبوعي",
        "monthly": "شهري",
        "day_of_month": "يوم محدد بالشهر",
        "quarterly": "ربع سنوي",
        "semiannual": "نصف سنوي",
        "annual": "سنوي",
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
