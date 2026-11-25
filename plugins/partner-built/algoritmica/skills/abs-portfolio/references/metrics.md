# SME ABS data and metric definitions

Read this reference when mapping inputs, calculating a measure or interpreting the
report. The helper computes descriptive quantities from observed records. It does
not price bonds, infer cash-flow waterfalls or allocate an investor's ownership of
collateral.

## Inputs and grain

`analyse_portfolio.py` accepts a version 1 snapshot from `deeploans_api.py`, a
holdings CSV and an explicit reporting date. It uses
[`sme-api-fields.json`](sme-api-fields.json) to map verified raw source columns to
logical fields. The snapshot retains raw API values and coverage metadata. A local
export must use the same snapshot envelope and carry an accurate coverage manifest;
absence of that metadata means completeness is unverified.

Holdings CSV columns:

| Column | Meaning |
| --- | --- |
| `position_id` | Required unique identifier for the position |
| `deal_id` | Required explicit Deeploans `dl_code`; no guessed name matching |
| `tranche_id` | Tranche class matching `bond_tranches.BS25`; supply this or `security_id` |
| `security_id` | Security identifier matching `bond_tranches.BS26`; if both identifiers are supplied, both must agree |
| `position_balance` | Required signed position principal amount in currency units, not a price or weight |
| `currency` | Required three-letter currency code; must agree with the matched tranche currency when supplied there |
| `market_value` | Optional signed position market value supplied by the user in the position currency; never inferred from principal |

The helper does not independently verify valuation dates for supplied market
values. It flags that limitation as a metric coverage gap rather than treating the
reporting cutoff as an implied valuation date.

The fixture identifiers beginning `TEST-` are synthetic, not real securities.
Separate positions can reference the same tranche. Duplicate `position_id` records
are quarantined in their entirety, rather than summed or resolved by row order.

## Linkage and exact dates

Holdings match the tuple `(deal_id, tranche_id)` and/or the security identifier in
the same deal. Securities with ambiguous matches remain unmapped. Source tranche
rows must match the requested cutoff exactly. The current `deals` endpoint has no
verified deal identifier in the checked-in schema, so the helper uses explicit
deal keys from `bond_tranches` and `loans`; it does not invent a join to `deals`.

Loan identity is `(deal_id, pool_id, loan_id, report_date)`. Loans, performances and
obligor attributes join on the exact cutoff and loan identity. An ISO date or ISO
timestamp is accepted after validating the whole timestamp; ambiguous local dates
or invalid timestamp suffixes are not. No stale cutoff is silently
carried forward. All occurrences of duplicate keys are excluded. Missing
performance observations remain unknown, not current or zero.

The collateral view includes observed loans in each mapped held deal once per
cutoff, even when several held tranches reference that deal. A shared deal key
does not prove the legal allocation of pools to tranches. Report this as a
**deal-level collateral look-through**, not a claim that the investor owns every
loan or a specified fraction of it. No collateral balance is multiplied by the
number of held tranches.

## Coverage and currencies

Every supplied table's declared `row_count` is reconciled with the actual row
array. Missing, negative, noninteger or mismatched counts prevent a completeness
claim. All four tables used by reported measures (`loans`, `bond_tranches`,
`performances`, `obligors`) need explicit completeness metadata. The unused
`deals` reference table does not need a complete flag, but any declared row count
must still reconcile. A consistent row count is a necessary input check, not proof
that a live API snapshot is complete.

The present API uses unordered offset pagination without a count, cursor or stable
snapshot contract. API input therefore remains `partial_or_unverified` even when
all requested offsets were exhausted. Manually setting its top-level complete
flag does not promote an API snapshot to complete. A complete synthetic fixture or
independently reconciled export is a separate input scope, not evidence that live
API retrieval was complete.

Every monetary aggregate is grouped by currency. There is no implicit FX
conversion and no cross-currency portfolio total. The report distinguishes
supplied position balances, known supplied market values and observed collateral
balances. Missing market value is counted and never filled using par or price
assumptions. Decimal arithmetic is used; monetary quantities and ratios are
serialized as decimal strings. Ratios are fractions, so `0.2` means 20%.

## Calculations

- **Observed collateral balance:** sum of finite, nonnegative `loans.AS55` values
  for usable, unique loans at the exact cutoff. Rows with invalid balances or
  missing currencies are excluded and reported as exceptions.
- **Concentration:** each group's observed balance divided by the observed
  balance in the same currency. Unknown classifications appear explicitly as
  `UNKNOWN`. Dimensions include deal, pool, originator, servicer, country, sector,
  origination year and obligor. These are collateral concentrations, not weighted
  holdings allocations. Attribute meanings follow the configured source mapping.
- **Arrears buckets:** principal days (`AS118`) and interest days (`AS116`) are
  treated separately: current (0), 1-30, 31-60, 61-90, over 90, and unknown. Invalid,
  fractional or negative days are unknown. Bucket shares divide by observed
  balance, including unknown-status loans in the denominator. Principal arrears
  amount (`AS117`) and interest arrears amount (`AS115`) are summed only where
  known and nonnegative; known and missing row counts are included.
- **Transaction default flag:** only JSON boolean `AS121` values are interpreted.
  `true` balance divided by known boolean-status balance is a descriptive flagged
  balance share. It is not a default incidence rate. Strings such as `Y`, numeric
  values and missing statuses remain unclassified, with raw distributions
  available. Basel flag `AS122` is a different definition and is not substituted.
- **Weighted interest rate:** absent by default because source rate units are not
  verified. A custom mapping can add `loans.interest_rate` and top-level
  `interest_rate_unit` equal to `decimal` or `percent`. Valid supplied rates are
  weighted by positive observed balances with known rates; percent values are
  converted to fractions. Known balance and row counts accompany the result.
  Different currencies remain separate.
- **Two-cutoff movements:** difference between observed currency balances only
  when both cutoffs have usable observations. Missing cutoffs produce unavailable
  values, not changes from zero. The helper separately reports common/new/no-longer
  observed loan counts and balance changes on common, same-currency loans. These
  do not establish prepayments, defaults, losses or causation.

CPR/CDR, annualised loss rates, expected loss, OAS, yield, duration, DV01,
contractual waterfall allocations and investment recommendations are outside this
helper. Compute additional measures only from explicitly supplied definitions and
adequate data, using a separately validated calculation implementation.

## Outputs and reproducibility

`report.json` contains holdings mappings, coverage, detailed per-currency
concentrations, arrears/default summaries, optional cutoff comparisons, exceptions
and calculation boundaries. `report.md` presents the principal results for review.
The CLI records SHA-256 checksums of the snapshot, holdings, mapping and helper so
the same run can be reproduced. Sensitive source data should remain in the
customer's chosen working environment; synthetic fixtures are the committed
example inputs.

Dataset completeness and metric availability are reported separately. A complete
synthetic input scope can still omit prices, status definitions or classifications;
`metric_coverage.gaps` and the Markdown report list those omissions explicitly. The
standalone Markdown prominently labels synthetic fixture data and includes
whitelisted source provenance (`kind`, `fetched_at`, sanitized deployment URL and
relevant scope metadata). Credential fields, URL credentials, queries and fragments
are not copied into generated source metadata.

Run the offline example from the plugin directory:

```bash
python3 skills/abs-portfolio/scripts/analyse_portfolio.py \
  --snapshot skills/abs-portfolio/tests/fixtures/sme-snapshot.json \
  --holdings skills/abs-portfolio/tests/fixtures/holdings.csv \
  --as-of 2026-11-30 --compare-as-of 2026-10-31 \
  --output-dir /tmp/abs-portfolio-example
```

The example has three positions in two deals, with two held tranches sharing one
EUR pool. Its independently counted current collateral is EUR 100 and USD 50;
position principal is EUR 50 and USD 20. The previous observed collateral is EUR
120 and USD 60. The fixture deliberately omits one position market value and
includes an unclassified default-status string to demonstrate coverage reporting.
