"""Banking provider layer (Neotek B2B): every real money movement goes through here.

Model agreed with Neotek: one Master Account in Miftah Alkhazina's name, with a virtual IBAN per client
company under it. The client transfers its revenue (e.g. a standing order from its own bank) to its virtual
IBAN; the money lands in the master and Neotek notifies us. The split into items is kept in our ledger only,
so the one movement we ask the provider for is the payout: master -> beneficiary IBAN, single or in bulk,
always in Miftah's name with a description naming the client and the purpose.

The rest of the system only knows `provider()`. Today the provider is a simulation that always succeeds;
when Neotek's documentation arrives, a `NeotekProvider` with the same methods is swapped in here only.
"""
from __future__ import annotations

import uuid
from typing import Protocol


class BankingError(RuntimeError):
    """The provider rejected the operation (balance, IBAN, outage...)."""


class BankingProvider(Protocol):
    def master_ref(self) -> str:
        """The provider's id for Miftah's master account (where every client's money is held)."""

    def transfer(self, *, from_ref: str, to_iban: str, amount: int, reference: str, description: str = "") -> str:
        """Payout of `amount` halalas from the master to a beneficiary IBAN. Returns the provider's operation id."""


class SimulatedProvider:
    """Simulation: transfers succeed immediately."""

    name = "simulated"

    def master_ref(self) -> str:
        return "sim-master"

    def transfer(self, *, from_ref: str, to_iban: str, amount: int, reference: str, description: str = "") -> str:
        if amount <= 0:
            raise BankingError("يجب أن يكون مبلغ التحويل أكبر من صفر")
        return f"sim-tr-{uuid.uuid4().hex[:12]}"


_provider: BankingProvider = SimulatedProvider()


def provider() -> BankingProvider:
    return _provider


def is_simulated() -> bool:
    return isinstance(_provider, SimulatedProvider)
