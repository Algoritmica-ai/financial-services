# Algoritmica ABS Portfolio Plugin

Review SME-backed ABS holdings and their underlying collateral using the direct Deeploans API. The plugin combines a scoped analysis skill with a read-only API client and deterministic Python calculations. It does not require an external analytics provider or a Deeploans MCP server.

## What it analyses

- Holdings by deal, tranche and currency, using positions supplied by the user.
- Underlying SME loan balances, concentrations and available arrears/default fields.
- Changes between requested reporting dates, with population and coverage differences identified.
- Data-quality exceptions, missing identifiers and unmatched holdings.

The collateral view counts shared pools once, even if several held tranches refer to them. Collateral balances are not presented as the investor's owned exposure. Market values are reported only when supplied. This release does not price securities, simulate contractual waterfalls or forecast tranche returns.

## Install

Add this fork as a marketplace and install its Algoritmica plugin:

```bash
claude plugin marketplace add Algoritmica-ai/financial-services
claude plugin install algoritmica@algoritmica-financial-services
```

In Cowork, add `https://github.com/Algoritmica-ai/financial-services` as a plugin marketplace and select **Algoritmica**. The skill is also available as the standalone folder `skills/abs-portfolio/`; install that folder through your host's skill installation mechanism when using a different agent host.

Invoke `/algoritmica:review-abs-portfolio` or ask the assistant to use `abs-portfolio`. Provide the holdings file, exact reporting date and optional comparison date. Runtime helpers require Python 3.10 or later and use the standard library.

## Data access

Set `DEEPLOANS_API_URL` to the base URL of your Deeploans deployment and `DEEPLOANS_API_KEY` to its user API key. Keep the key in the environment, not in a prompt, command argument or committed file. Read [API access and mapping](skills/abs-portfolio/references/api.md) for the supported request arguments and field names. No production endpoint or credentials are bundled.

The current checked-in Deeploans API documents SME endpoints. Other ETL asset classes in Deeploans do not imply that the same API exposes them. This plugin leaves the Deeploans backend unchanged.

The API uses offset pagination without a documented stable order, row count or snapshot token. The client can establish that it reached an empty page, but cannot independently prove that the dataset is complete and consistent. It preserves that limitation in every API snapshot. Analyses based on those snapshots are provisional; a saved API snapshot does not become complete merely because it is stored locally.

The synthetic fixture used by the tests is a controlled local dataset with known contents. It demonstrates calculations and error handling and makes no claim about a real portfolio's performance.

## Reproduce the analysis

Run the helpers with `--help` for their full argument definitions:

```bash
python3 skills/abs-portfolio/scripts/deeploans_api.py --help
python3 skills/abs-portfolio/scripts/analyse_portfolio.py --help
```

Run the offline synthetic example from the plugin directory:

```bash
python3 skills/abs-portfolio/scripts/analyse_portfolio.py \
  --snapshot skills/abs-portfolio/tests/fixtures/sme-snapshot.json \
  --holdings skills/abs-portfolio/tests/fixtures/holdings.csv \
  --as-of 2026-11-30 --compare-as-of 2026-10-31 \
  --output-dir /tmp/abs-portfolio-example
```

These are illustrative reporting dates. The fixture contains three held securities across two deals, including two tranches sharing one EUR pool. Its current collateral balances are EUR 100 and USD 50; supplied position principal is EUR 50 and USD 20. They are different measures.

The report includes a machine-readable JSON result and a Markdown review. [Metric definitions](skills/abs-portfolio/references/metrics.md) describe weighting, currencies, cutoffs and exclusions.

## Validation

From the repository root:

```bash
python3 -m unittest discover -s plugins/partner-built/algoritmica/skills/abs-portfolio/tests -v
```

Tests use local synthetic data and an HTTP test server. They do not contact a live Deeploans deployment or require credentials. They check pagination, coverage limitations, source parsing, holdings linkage, shared pools, dates, currencies, duplicates and financial aggregates.

## Source and licence

The skill adapts the workflow structure of the upstream `fixed-income-portfolio` skill. Algoritmica replaces its provider-specific tools and pricing workflow with Deeploans SME data retrieval, holdings/collateral reconciliation and deterministic analysis. See the [skill's provenance](skills/abs-portfolio/SKILL.md#provenance) and the repository [Apache-2.0 licence](../../../LICENSE).
