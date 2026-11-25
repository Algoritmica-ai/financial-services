# Deeploans SME API contract

The client in `../scripts/deeploans_api.py` uses the API directly. It does not
require a Deeploans MCP server or any market-data connector.

## Configuration

Use Python 3.10 or newer. The retrieval client uses the standard library only.
Configure `DEEPLOANS_API_URL` with the deployment root, such as
`https://your-deployment.example` or `http://127.0.0.1:8000` for local testing.
Include a mount prefix if the deployment has one; exclude `/api/v1/sme`.
Set `DEEPLOANS_API_KEY` through the execution environment. Never put credentials
in the skill, a command argument, a snapshot or a report.

HTTPS is required except for loopback HTTP. The client blocks redirects and does
not include server response bodies or credentials in public error messages.
All requests are GET requests to an allowlist of documented SME endpoints.

From the skill directory:

```bash
python3 scripts/deeploans_api.py \
  --tables loans performances obligors bond_tranches \
  --filter 'loans=dl_code:EXAMPLE001' \
  --filter 'performances=dl_code:EXAMPLE001' \
  --filter 'obligors=dl_code:EXAMPLE001' \
  --filter 'bond_tranches=dl_code:EXAMPLE001' \
  --output /tmp/sme-observations.json
```

This command deliberately exits `3` after exhausted retrieval. When orchestrating
it from Python, use `subprocess.run(command, check=False)` and inspect
`result.returncode`: continue to explicitly provisional analysis only when it is
`3`. Treat `2` as a retrieval/configuration failure and inspect the saved coverage
diagnostics, if an output was written. Preserve the coverage metadata when passing
the snapshot to the analysis helper. A shell command joined with `&&` or a process
runner using `check=True` would stop on the intended provisional exit status.

`EXAMPLE001` is an illustrative identifier, not a live deal. Choose the scope
from the user's actual holdings and available identifiers. Filters are specified
per table; a filter on loans does not restrict performances or bond tranches.
Apply equivalent deal and cutoff restrictions wherever supported. The API's
filter parser uppercases text values. `--columns TABLE=COL1,COL2` is available,
but preserve all identifiers, dates, balances and fields required by the analysis.

## Endpoints and responses

The verified source registers `/api/v1/sme/{table}` routes. Allowed tables are
`amortisation_profiles`, `bond_collaterals`, `bond_info`, `bond_tranches`, `deals`,
`financials`, `interests`, `loan_collaterals`, `loans`, `nace_codes`, `nuts3_codes`,
`obligors` and `performances`.

The client sends `detailed=true`, `limit` and `offset`, with optional `filter`
and `columns`. Authentication uses the `x-algoritmica-api-key` request header.
The detailed response is a JSON array of row objects. A `data` or `results`
wrapper and a total-count property are not part of this source contract.
Responses of a different shape are rejected instead of guessed.

Filters use the existing API syntax, for example `dl_code:EXAMPLE001` and
`AS1:2026-10-31`, with `AND`/`OR` separators. Filterable fields differ by table;
in particular `BS1` is not marked filterable in the checked-in tranche schema.
Do not assume every field supports every operator.

## Retrieval is not verified completeness

The current backend generates `SELECT ... LIMIT ... OFFSET ...` **without an
ORDER BY clause, cursor, snapshot identifier or independent total count**.
Omitting `limit` uses a configured default; it is not an unrestricted export.
The server can also clamp a supplied limit.

The client therefore continues after short pages, advancing offsets by the rows
actually observed. It records `pagination_exhausted=true` only after an empty
page. Repeated pages, invalid responses, row/page caps and request failures stop
retrieval and remain explicit in the snapshot.

Even exhausted pagination always leaves `coverage.complete=false` and
`completeness_verified=false`. Rows may be duplicated or omitted by unordered
offset queries. The observations may be used for explicitly provisional subset
analysis; they must not be presented as verified portfolio totals. A complete
portfolio review needs an independently verified fixed export or a backend
contract with stable ordering, snapshot consistency and completeness checks.
Changing a metadata flag manually is not verification.

Exit codes:

- `3`: observations saved and offsets exhausted; completeness remains unverified.
- `2`: invalid configuration, a request failure or a retrieval safety limit.

There is deliberately no successful-completeness exit code for this API contract.
Inspect `coverage.tables` before using any saved observations. The client stops
remaining table requests after a network/authentication/redirect error.

## Snapshot and field mapping

The JSON snapshot contains `schema_version: 1`, a credential-free `source`, raw
rows under `tables`, and explicit overall/per-table `coverage`. Each retrieved
table records its observed row count, successful request count, SHA-256 hash,
pagination state and termination reason. Hashes identify the saved observations;
they do not independently prove source completeness.

[`sme-api-fields.json`](sme-api-fields.json) maps verified raw fields to logical
analysis fields. Null means no verified corresponding field, not zero.
Loan balances and currency come from `loans`; arrears and default flags come from
`performances`. Join using deal, pool, loan and report date where available.
`deals` has no verified deal identifier in the checked-in API schema and must not
be joined using an invented field. A tranche's `BS25` is its class name and
`BS26` is its ISIN; scope either by deal and cutoff.

The transaction default flag `AS121` and Basel flag `AS122` are separate fields.
The source declares booleans but this mapping does not invent a broader coded
status vocabulary. Interest rate `AS80` is verified by name; its scale must be
confirmed separately before aggregating rates.

## Verified upstream sources

- [SME routes](https://github.com/Algoritmica-ai/deeploans/blob/main/api/api-backend-main/backend/app/routes/sme.py)
- [SQL construction and response shape](https://github.com/Algoritmica-ai/deeploans/blob/main/api/api-backend-main/backend/app/big_query_useful.py)
- [API table fields and filters](https://github.com/Algoritmica-ai/deeploans/blob/main/api/api-backend-main/backend/app/files/tables_with_filters.json)
- [Filter and column parser](https://github.com/Algoritmica-ai/deeploans/blob/main/api/api-backend-main/backend/app/filters/parser.py)
- [SME field semantics](https://github.com/Algoritmica-ai/deeploans/tree/main/etl-pipelines/ECB-Loan-level-data-templates/SME-Loans-ETL-Pipeline/SME-Looker/views)

These sources were inspected during implementation. No live customer API or
portfolio credentials were used to validate deployment availability.
