"""طبقة مزوّد الربط البنكي (نيوتك): فتح الحسابات الافتراضية والتحويل للمستفيدين.

كل اللي يعرفه باقي النظام هو `provider()`. اليوم المزوّد محاكاة تنجح دائمًا،
ولما توصل وثائق نيوتك ينضاف `NeotekProvider` بنفس الدالتين ويتبدل من هنا بس.
"""
from __future__ import annotations

import uuid
from typing import Protocol


class BankingError(RuntimeError):
    """المزوّد رفض العملية (رصيد، آيبان، انقطاع...)."""


class BankingProvider(Protocol):
    def open_virtual_account(self, entity_id: int, bucket_id: int, name: str) -> tuple[str, str]:
        """يفتح حساب افتراضي تحت الحساب المجمّع للشركة. يرجّع (الآيبان، معرّفه عند المزوّد)."""

    def transfer(self, *, from_ref: str, to_iban: str, amount: int, reference: str) -> str:
        """يحوّل `amount` هللة من حساب افتراضي لآيبان مستفيد. يرجّع رقم العملية عند المزوّد."""


class SimulatedProvider:
    """محاكاة: آيبان افتراضي ثابت لكل بند، والتحويل ينجح فورًا."""

    name = "simulated"

    def open_virtual_account(self, entity_id: int, bucket_id: int, name: str) -> tuple[str, str]:
        # SA + 22 رقم: «90» تميّز الافتراضي، ثم رقم الشركة ورقم البند
        iban = f"SA90{entity_id:08d}{bucket_id:012d}"
        return iban, f"sim-va-{entity_id}-{bucket_id}"

    def transfer(self, *, from_ref: str, to_iban: str, amount: int, reference: str) -> str:
        if amount <= 0:
            raise BankingError("مبلغ التحويل لازم يكون أكبر من صفر")
        return f"sim-tr-{uuid.uuid4().hex[:12]}"


_provider: BankingProvider = SimulatedProvider()


def provider() -> BankingProvider:
    return _provider


def is_simulated() -> bool:
    return isinstance(_provider, SimulatedProvider)
