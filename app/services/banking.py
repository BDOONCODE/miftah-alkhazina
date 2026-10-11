"""Banking provider layer (Neotek): opening virtual accounts and transferring to beneficiaries.

The rest of the system only knows `provider()`. Today the provider is a simulation that always succeeds;
when Neotek's documentation arrives, a `NeotekProvider` with the same two methods is swapped in here only.
"""
from __future__ import annotations

import uuid
from typing import Protocol


class BankingError(RuntimeError):
    """The provider rejected the operation (balance, IBAN, outage...)."""


class BankingProvider(Protocol):
    def open_virtual_account(self, entity_id: int, bucket_id: int, name: str) -> tuple[str, str]:
        """Open a virtual account under the company's pool account. Returns (IBAN, provider id)."""

    def transfer(self, *, from_ref: str, to_iban: str, amount: int, reference: str) -> str:
        """Transfer `amount` halalas from a virtual account to a beneficiary IBAN. Returns the provider's operation id."""


class SimulatedProvider:
    """Simulation: a fixed virtual IBAN per item, and transfers succeed immediately."""

    name = "simulated"

    def open_virtual_account(self, entity_id: int, bucket_id: int, name: str) -> tuple[str, str]:
        # SA + 22 digits: "90" marks it as virtual, then the company id and the item id
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
