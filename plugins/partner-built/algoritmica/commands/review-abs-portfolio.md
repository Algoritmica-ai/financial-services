---
description: Analyse SME-backed ABS holdings and collateral using the Deeploans API
argument-hint: "<holdings.csv> <as-of YYYY-MM-DD> [comparison date]"
---

# Review ABS Portfolio

Use the **abs-portfolio** skill at [SKILL.md](../skills/abs-portfolio/SKILL.md).

Establish the holdings file, reporting cutoff, optional comparison cutoff, and the configured Deeploans API location. Retrieve complete underlying SME data where available, calculate holdings composition and collateral performance, and produce a report with calculation definitions, source dates, linkage coverage and data exceptions.

Use direct read-only Deeploans API requests and the supplied calculation scripts. The first release supports SME-backed securitisations. Identify unavailable data explicitly; do not substitute a different asset class or invent prices, risk measures, projected cash flows or waterfall results.
