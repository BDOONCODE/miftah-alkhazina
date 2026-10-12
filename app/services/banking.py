"""Banking provider layer (Neotek): every real money movement goes through here.

Flow per incoming amount:
  1. sweep:    company bank account (where the money landed) -> the company's pool virtual account
  2. move:     pool virtual account -> each item's virtual account (the split)
  3. transfer: item virtual account (or the pool, for the surplus) -> beneficiary IBAN, when due

The rest of the system only knows `provider()`. Today the provider is a simulation that always succeeds;
when Neotek's documentation arrives, a `NeotekProvider` with the same methods is swapped in here only.
"""
from __future__ import annotations

import uuid
from typing import Protocol


class BankingError(RuntimeError):
    """The provider rejected the operation (balance, IBAN, outage...)."""


class BankingProvider(Protocol):
    def open_pool_account(self, entity_id: int, name: str) -> tuple[str, str]:
        """Open the company's pool virtual account, where all incoming money collects. Returns (IBAN, provider id)."""

    def sweep(self, *, from_iban: str, to_ref: str, amount: int, reference: str) -> str:
        """Move `amount` halalas from a company bank account into the pool virtual account. Returns the operation id."""

    def move(self, *, from_ref: str, to_ref: str, amount: int, reference: str) -> str:
        """Move `amount` halalas between two of the company's virtual accounts. Returns the operation id."""

    def open_virtual_account(self, entity_id: int, bucket_id: int, name: str) -> tuple[str, str]:
        """Open a virtual account under the company's pool account. Returns (IBAN, provider id)."""

    def transfer(self, *, from_ref: str, to_iban: str, amount: int, reference: str) -> str:
        """Transfer `amount` halalas from a virtual account to a beneficiary IBAN. Returns the provider's operation id."""


class SimulatedProvider:
    """Simulation: a fixed virtual IBAN per item, and transfers succeed immediately."""

    name = "simulated"

    def open_pool_account(self, entity_id: int, name: str) -> tuple[str, str]:
        # SA + 22 digits: "91" marks the pool, then the company id
        return f"SA91{entity_id:08d}{0:012d}", f"sim-pool-{entity_id}"

    def sweep(self, *, from_iban: str, to_ref: str, amount: int, reference: str) -> str:
        return self._operation(amount, "sw")

    def move(self, *, from_ref: str, to_ref: str, amount: int, reference: str) -> str:
        return self._operation(amount, "mv")

    @staticmethod
    def _operation(amount: int, kind: str) -> str:
        if amount <= 0:
            raise BankingError("يجب أن يكون مبلغ العملية أكبر من صفر")
        return f"sim-{kind}-{uuid.uuid4().hex[:12]}"

    def open_virtual_account(self, entity_id: int, bucket_id: int, name: str) -> tuple[str, str]:
        # SA + 22 digits: "90" marks it as virtual, then the company id and the item id
        iban = f"SA90{entity_id:08d}{bucket_id:012d}"
        return iban, f"sim-va-{entity_id}-{bucket_id}"

    def transfer(self, *, from_ref: str, to_iban: str, amount: int, reference: str) -> str:
        if amount <= 0:
            raise BankingError("يجب أن يكون مبلغ التحويل أكبر من صفر")
        return f"sim-tr-{uuid.uuid4().hex[:12]}"


_provider: BankingProvider = SimulatedProvider()


def provider() -> BankingProvider:
    return _provider


def is_simulated() -> bool:
    return isinstance(_provider, SimulatedProvider)
