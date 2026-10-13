import hashlib

from fastapi.templating import Jinja2Templates

from . import i18n
from .config import BASE_DIR
from .domain.money import format_amount, format_percent
from .domain.periods import RIYADH
from .services.accounts import KIND_LABELS
from .services.payouts import REASON_LABELS
from .services.policies import APPROVAL_MODES
from .web import pop_flashes

templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
# Arabic / English: static text and printed values are translated at render time (see app/i18n.py)
templates.env.add_extension(i18n.ArabicText)
templates.env.finalize = i18n.finalize
templates.env.globals.update(
    _td=i18n._td, _ts=i18n._ts, lang=lambda: i18n.current_lang.get(), js_catalog=i18n.js_catalog,
)
templates.env.filters["keep"] = lambda v: i18n.Keep(v) if isinstance(v, str) else v
templates.env.filters["sar"] = format_amount
templates.env.filters["pct"] = format_percent
templates.env.filters["riyadh"] = lambda dt, fmt="%Y-%m-%d %H:%M": dt.astimezone(RIYADH).strftime(fmt) if dt else ""
templates.env.globals["pop_flashes"] = pop_flashes


def _asset_version() -> str:
    """A version number that changes with every static file update, so browsers fetch the new copy instead of a cached one."""
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
    ROLE_LABELS={"admin": "المدير العام", "accountant": "محاسب", "approver": "مراجع"},
    CALC_LABELS={"percentage": "نسبة", "fixed_amount": "مبلغ ثابت", "vat_inclusive": "ضريبة القيمة المضافة"},
    FREQ_LABELS={
        "immediate": "فوري (بدون موعد)",
        "daily": "يومي",
        "weekly": "أسبوعي",
        "monthly": "شهري",
        "day_of_month": "يوم محدد من الشهر",
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
        "FULLY_FUNDED": "مموَّل بالكامل",
        "PARTIAL": "مموَّل جزئيًا",
        "UNFUNDED": "غير مموَّل",
        "NOT_REQUIRED": "غير مطلوب",
    },
)
