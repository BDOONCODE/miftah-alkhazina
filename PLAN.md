# Miftah Alkhazina: the approved MVP plan

> A configurable Profit First system: every incoming amount is split immediately according to a policy the accountant defines per company, as a priority-ordered waterfall.
> A new project from scratch. **All tools are free and open source.**

## Decisions

| Topic | Decision |
|---|---|
| Stack | Python 3.12 + FastAPI + SQLAlchemy 2 + Alembic + pytest. Jinja2 UI with hand-written RTL CSS and vanilla JS (no build step) |
| Database | SQLite for local development, Supabase (PostgreSQL, free plan) in production, switched by `DATABASE_URL` only |
| Hosting | Free hosting (Render) on a free subdomain. Demo data only until data-residency requirements in Saudi Arabia are reviewed |
| "Immediate" period | A perpetual period that never closes, so an immediate item's deficit carries until paid |
| Amounts | Integers in halalas. Rates round down and the remainder goes to surplus; VAT rounds to the nearest halala like an invoice |
| Percentage item | Computed from the full transaction amount M; its deficit **carries** to later transactions until the end of its period |
| VAT item | Optional item the accountant adds. The incoming amount already includes the tax, so its share is M x 15 / 115 |
| Fixed item | A target per period. With a due date it accrues day by day and completes before the due date; without one it takes the whole target at once. At period end the remaining deficit is recorded |
| Surplus | Recorded as **surplus** (not an item) and shown on the dashboard. The accountant chooses where it goes, or it stays in the pool |
| Users | Login and roles: admin / accountant / reviewer, plus self-signup. Approval per company is either self-approval or an independent reviewer (four eyes) |
| Workflow | The accountant registers the company (CR, VAT number, bank, IBAN) and optionally a reviewer → adds bank accounts → builds the policy and submits it → it is approved → every incoming amount is split by the policy |
| Payouts | Each item's share is held in a virtual account at the banking provider (in the background) and transferred to its beneficiary: immediately for undated items, in full on the due date otherwise |
| Reports | Weekly / monthly / yearly reports, downloadable as Excel (openpyxl) |
| Dashboard | Configurable, **per company and shared**, one dashboard per company, with **custom formula metrics** |
| Periods | Asia/Riyadh time, Gregorian calendar, weeks start on Sunday. If a set day does not exist in a month, its last day is used |

## The engine

The engine is a pure function, isolated from the database:
`allocate(buckets, period_states, amount, on) -> AllocationResult`

```
remaining = M
for each item by priority:
  percentage: required = floor(rate x M) + carried_deficit
  VAT:        required = round(M x rate / (1 + rate)) + carried_deficit
  fixed:      required = accrued_target(today) - funded_this_period
  allocated = min(required, remaining);  remaining -= allocated
  status: FULLY_FUNDED | PARTIAL | UNFUNDED | NOT_REQUIRED; carried_deficit = required - allocated (rate items)
surplus = remaining
invariant: sum(allocated) + surplus == M
```

## Hard rules

1. Records are append-only. Corrections use a reversing entry (`reversal_of`), never deletes or edits.
2. Allocations for one entity are serialized (entity-level lock).
3. `external_ref` is unique, so the same bank transaction is never processed twice.
4. Back-dated transactions are accepted while their period is open; later results are not recomputed.
5. Policy validation: rate total (VAT counted at its real share) <= 100%, unique priorities, required due dates.
6. Changing a protected item after it was funded in the current period (value, priority, schedule, beneficiary, deletion) needs a written justification of at least 20 characters.
7. When the policy changes an item keeps its balance, because its identity is stable across versions in the `buckets` table.
8. A reversal is blocked once any of the transaction's money was transferred to a beneficiary.

## Data model

- **Users and access:** `users`, `user_entities`
- **Entities:** `entities` (one company; `parent_id` is reserved for later)
- **Bank accounts:** `bank_accounts` (source, pool, other company account, external)
- **Policies:** `policies` (draft / pending / active / archived / rejected), `buckets` (stable identity, virtual account reference), `policy_buckets` (item settings per version)
- **Transactions:** `transactions` (amount, occurred_at, source, external_ref, reversal_of, created_by)
- **Allocation and state:** `allocation_events`, `bucket_period_state`, `period_closures`
- **Payouts:** `payouts` (transfers from an item, or the surplus, to a beneficiary)
- **Dashboard:** `dashboards`, `dashboard_widgets`, `custom_metrics`
- **Audit:** `audit_log`

## Configurable dashboard

- **Grid:** GridStack.js (MIT). **Charts:** Apache ECharts (Apache 2.0).
- **Widgets:** KPI card, item progress, allocation split, time series, deficit list, latest transactions.
- **Ranges:** current, previous, this month, this year, custom.
- **Custom metrics:** an arithmetic formula over the built-in metrics, e.g. `الفائض / الوارد * 100`, parsed by a safe `ast` allow-list (numbers, metrics, `+ - x /`, parentheses, min/max/abs). **Never eval.** Division by zero shows "—".
- **Permissions:** accountants edit dashboards and metrics; reviewers and others view them. Data is scoped by `user_entities`.

## Progress

- [x] Foundation: tables, Alembic, login and roles, `create-admin`
- [x] Engine with tests, including a property test of the invariant
- [x] Company and reviewer registration, policies (build, validate, submit, approve/reject, versions, protection)
- [x] Transactions: manual entry, bank webhook, bank simulator, reversal of the latest transaction
- [x] Dashboard, reports (Excel), audit log
- [x] Deployment (Supabase + Render)
- [x] Bank accounts, beneficiaries, due-date accrual, payouts (simulated), VAT item
- [ ] Real Neotek integration (virtual accounts + payouts), waiting on their API documentation

## Out of scope for now

Real bank transfers until the Neotek integration, purchase VAT netting, classifying incoming amounts (sales vs owner deposits), entity hierarchies, multiple currencies, per-accountant dashboards, and cross-company dashboards.
