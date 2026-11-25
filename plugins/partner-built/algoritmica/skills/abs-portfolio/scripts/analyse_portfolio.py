#!/usr/bin/env python3
"""Reproducible SME ABS holdings and collateral review; standard library only."""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit, urlunsplit


class AnalysisError(ValueError):
    """An input cannot be interpreted without inventing financial information."""


def number(value):
    """Accept finite decimal quantities; never treat booleans as 0/1."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def amount(value):
    if value is None:
        return None
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def ratio(numerator, denominator):
    if denominator <= 0:
        return None
    with localcontext() as context:
        context.prec = 28
        return amount(numerator / denominator)


def identifier(value):
    return str(value).strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""


def currency_code(value):
    code = identifier(value).upper()
    return code if len(code) == 3 and all("A" <= character <= "Z" for character in code) else ""


def cutoff(value):
    if not isinstance(value, str):
        return None
    try:
        if len(value) > 10 and value[10] in ("T", " "):
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
            result = parsed.date().isoformat()
            return result if value[:10] == result else None
        result = date.fromisoformat(value).isoformat()
        return result if result == value else None
    except ValueError:
        return None


def source_provenance(source):
    """Copy only documented noncredential provenance fields into generated reports."""
    result = {}
    for field in ("kind", "asset_class"):
        value = identifier(source.get(field))
        if value:
            result[field] = value[:100]
    fetched = source.get("fetched_at")
    if isinstance(fetched, str):
        try:
            result["fetched_at"] = datetime.fromisoformat(fetched[:-1] + "+00:00" if fetched.endswith("Z") else fetched).isoformat()
        except ValueError:
            result["fetched_at"] = "unavailable"
    if isinstance(source.get("ordered_snapshot"), bool):
        result["ordered_snapshot"] = source["ordered_snapshot"]
    if isinstance(source.get("base_url"), str):
        try:
            parsed = urlsplit(source["base_url"])
            if parsed.scheme in ("https", "http") and parsed.hostname:
                host = parsed.hostname
                if ":" in host:
                    host = "[" + host + "]"
                authority = host + (":" + str(parsed.port) if parsed.port else "")
                result["base_url"] = urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
        except ValueError:
            pass
    return result


def issue(exceptions, code, message, table=None, count=None):
    item = {"code": code, "message": message}
    if table:
        item["table"] = table
    if count is not None:
        item["count"] = count
    exceptions.append(item)


def mapped_table(snapshot, mapping, table):
    raw = snapshot.get("tables", {}).get(table, [])
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        raise AnalysisError(f"Table {table} must be a list of objects")
    fields = mapping.get("tables", {}).get(table, {})
    if not isinstance(fields, dict):
        raise AnalysisError(f"Field mapping for {table} must be an object")
    if any(value is not None and not isinstance(value, str) for value in fields.values()):
        raise AnalysisError(f"Field mapping for {table} must use column names or null")
    return [{name: row.get(column) for name, column in fields.items() if column} for row in raw]


def loan_key(row):
    return (identifier(row.get("deal_id")), identifier(row.get("pool_id")), identifier(row.get("loan_id")))


def unique_index(rows, key_function, table, exceptions):
    """Quarantine every occurrence of a duplicate key; ordering cannot choose a winner."""
    counts = Counter(key_function(row) for row in rows)
    duplicates = {key for key, count in counts.items() if count > 1}
    if duplicates:
        issue(exceptions, "duplicate_key", "All rows with duplicate keys were excluded", table,
              sum(counts[key] for key in duplicates))
    return {key_function(row): row for row in rows if key_function(row) not in duplicates}


def rows_at_date(rows, target, table, exceptions, allow_static=False):
    result = []
    missing = 0
    for row in rows:
        value = cutoff(row.get("report_date"))
        if value == target or (allow_static and not row.get("report_date")):
            result.append(row)
        elif value is None:
            missing += 1
    if missing:
        issue(exceptions, "invalid_report_date", "Rows without an unambiguous reporting date were excluded", table, missing)
    return result


def _holdings(holdings, tranche_rows, loan_rows, exceptions):
    if not holdings:
        raise AnalysisError("At least one holdings row is required")
    if any(not isinstance(row, dict) for row in holdings):
        raise AnalysisError("Holdings must be objects")
    missing_ids = sum(not identifier(row.get("position_id")) for row in holdings)
    if missing_ids:
        issue(exceptions, "missing_position_id", "Holdings without position_id were excluded", "holdings", missing_ids)
    candidates = [row for row in holdings if identifier(row.get("position_id"))]
    unique = unique_index(candidates, lambda row: identifier(row.get("position_id")), "holdings", exceptions)
    known_deals = {identifier(row.get("deal_id")) for row in tranche_rows + loan_rows}
    known_deals.discard("")
    valid_tranches = unique_index(
        [row for row in tranche_rows if identifier(row.get("deal_id")) and identifier(row.get("tranche_id"))],
        lambda row: (identifier(row.get("deal_id")), identifier(row.get("tranche_id"))),
        "bond_tranches", exceptions)
    security_index = defaultdict(list)
    for row in valid_tranches.values():
        if identifier(row.get("security_id")):
            security_index[identifier(row["security_id"])].append(row)
    output = []
    by_currency = defaultdict(lambda: {"position_balance": Decimal(0), "known_market_value": Decimal(0),
                                     "positions": 0, "positions_missing_market_value": 0})
    held_deals = set()
    for position_id, row in unique.items():
        deal = identifier(row.get("deal_id"))
        tranche = identifier(row.get("tranche_id"))
        security = identifier(row.get("security_id"))
        currency = currency_code(row.get("currency"))
        balance = number(row.get("position_balance"))
        market_value = number(row.get("market_value"))
        reason = None
        matched_tranche = valid_tranches.get((deal, tranche)) if tranche else None
        if not deal:
            reason = "missing_deal_id"
        elif deal not in known_deals:
            reason = "unknown_deal_id"
        elif not tranche and not security:
            reason = "missing_security_or_tranche"
        elif tranche and (deal, tranche) not in valid_tranches:
            reason = "unknown_tranche"
        elif security:
            matches = [match for match in security_index.get(security, []) if identifier(match.get("deal_id")) == deal]
            if len(matches) != 1 or (tranche and identifier(matches[0].get("tranche_id")) != tranche):
                reason = "unmatched_or_ambiguous_security"
            else:
                matched_tranche = matches[0]
        if balance is None:
            reason = reason or "invalid_position_balance"
        if not currency:
            reason = reason or "missing_position_currency"
        if matched_tranche and matched_tranche.get("currency") is not None:
            tranche_currency = currency_code(matched_tranche.get("currency"))
            if not tranche_currency:
                reason = reason or "invalid_tranche_currency"
            elif currency and currency != tranche_currency:
                reason = reason or "position_tranche_currency_mismatch"
        position = {"position_id": position_id, "deal_id": deal, "tranche_id": tranche or None,
                    "security_id": security or None, "currency": currency or None,
                    "position_balance": amount(balance), "market_value": amount(market_value),
                    "mapping_status": "unmapped" if reason else "mapped", "reason": reason}
        output.append(position)
        if reason:
            issue(exceptions, reason, f"Position {position_id} could not be fully mapped", "holdings")
        elif balance != 0:
            held_deals.add(deal)
        # Known holdings amounts are retained separately from collateral mapping coverage.
        if currency and balance is not None:
            bucket = by_currency[currency]
            bucket["positions"] += 1
            bucket["position_balance"] += balance
            if market_value is None:
                bucket["positions_missing_market_value"] += 1
            else:
                bucket["known_market_value"] += market_value
    for bucket in by_currency.values():
        for key in ("position_balance", "known_market_value"):
            bucket[key] = amount(bucket[key])
    return {"positions": output, "by_currency": dict(sorted(by_currency.items()))}, held_deals


def bucket_days(value):
    days = number(value)
    if days is None or days < 0 or days != days.to_integral_value():
        return "unknown"
    if days == 0:
        return "current"
    if days <= 30:
        return "1_30"
    if days <= 60:
        return "31_60"
    if days <= 90:
        return "61_90"
    return "over_90"


def group_balances(rows, field, total):
    groups = defaultdict(lambda: {"loan_count": 0, "balance": Decimal(0)})
    for row in rows:
        label = identifier(row.get(field)) or "UNKNOWN"
        group = groups[label]
        group["loan_count"] += 1
        group["balance"] += row["_balance"]
    return {key: {"loan_count": group["loan_count"], "balance": amount(group["balance"]),
                  "share_of_observed_balance": ratio(group["balance"], total)}
            for key, group in sorted(groups.items())}


def arrears_summary(rows, days_field, amount_field, total):
    buckets = defaultdict(lambda: {"loan_count": 0, "balance": Decimal(0)})
    arrears = Decimal(0)
    amount_known = 0
    for row in rows:
        item = buckets[bucket_days(row.get(days_field))]
        item["loan_count"] += 1
        item["balance"] += row["_balance"]
        value = number(row.get(amount_field))
        if value is not None and value >= 0:
            amount_known += 1
            arrears += value
    return {"buckets": {key: {"loan_count": item["loan_count"], "balance": amount(item["balance"]),
                               "share_of_observed_balance": ratio(item["balance"], total)}
                         for key, item in sorted(buckets.items())},
            "known_arrears_amount": amount(arrears) if amount_known else None,
            "arrears_amount_known_loan_count": amount_known,
            "arrears_amount_missing_or_invalid_loan_count": len(rows) - amount_known}


def _collateral(tables, held_deals, target, exceptions, interest_rate_unit=None):
    loans = rows_at_date(tables["loans"], target, "loans", exceptions)
    loans = [row for row in loans if identifier(row.get("deal_id")) in held_deals]
    missing_key = [row for row in loans if not loan_key(row)[0] or not loan_key(row)[2]]
    if missing_key:
        issue(exceptions, "missing_loan_key", "Loan rows with missing identifiers were excluded", "loans", len(missing_key))
    loans = [row for row in loans if loan_key(row)[0] and loan_key(row)[2]]
    loan_index = unique_index(loans, loan_key, "loans", exceptions)
    performance_rows = rows_at_date(tables["performances"], target, "performances", exceptions)
    performance_index = unique_index(
        [row for row in performance_rows if identifier(row.get("deal_id")) in held_deals],
        loan_key, "performances", exceptions)
    obligor_rows = rows_at_date(tables["obligors"], target, "obligors", exceptions)
    obligor_index = unique_index(
        [row for row in obligor_rows if identifier(row.get("deal_id")) in held_deals],
        loan_key, "obligors", exceptions)
    rows = []
    missing_performance = 0
    excluded = 0
    for key, loan in loan_index.items():
        row = dict(loan)
        performance = performance_index.get(key)
        if performance is None:
            missing_performance += 1
        for supplemental in (obligor_index.get(key, {}), performance or {}):
            for field, value in supplemental.items():
                if value is not None and field not in ("balance", "currency"):
                    if (field not in ("report_date", "deal_id", "pool_id", "loan_id")
                            and row.get(field) is not None and row[field] != value):
                        issue(exceptions, "conflicting_supplemental_field",
                              f"Conflicting {field} for loan {key}; original loan value retained", "loans")
                        continue
                    row[field] = value
        balance = number(loan.get("balance"))
        currency = currency_code(loan.get("currency"))
        if balance is None or balance < 0 or not currency:
            excluded += 1
            continue
        row["currency"] = currency
        row["_balance"] = balance
        row["vintage"] = cutoff(row.get("origination_date"))
        row["vintage"] = row["vintage"][:4] if row["vintage"] else None
        row["pool"] = identifier(row.get("deal_id")) + "/" + (identifier(row.get("pool_id")) or "UNKNOWN")
        rows.append(row)
    if missing_performance:
        issue(exceptions, "missing_performance", f"Loans at {target} without a matching performance row", "performances", missing_performance)
    if excluded:
        issue(exceptions, "invalid_collateral_balance_or_currency", "Loans without a nonnegative balance and currency were excluded", "loans", excluded)
    observed_deals = {identifier(row.get("deal_id")) for row in rows}
    for deal in sorted(held_deals - observed_deals):
        issue(exceptions, "missing_collateral_cutoff", f"No usable collateral for deal {deal} at exact cutoff {target}", "loans")
    by_currency = {}
    for currency in sorted({row["currency"] for row in rows}):
        selected = [row for row in rows if row["currency"] == currency]
        total = sum((row["_balance"] for row in selected), Decimal(0))
        default_true = sum((row["_balance"] for row in selected if row.get("default_status") is True), Decimal(0))
        known_default = [row for row in selected if isinstance(row.get("default_status"), bool)]
        known_default_balance = sum((row["_balance"] for row in known_default), Decimal(0))
        rate_rows = [(row, number(row.get("interest_rate"))) for row in selected] if interest_rate_unit in ("decimal", "percent") else []
        rate_rows = [(row, rate) for row, rate in rate_rows if rate is not None and row["_balance"] > 0]
        if interest_rate_unit == "percent":
            rate_rows = [(row, rate / Decimal(100)) for row, rate in rate_rows]
        rate_balance = sum((row["_balance"] for row, rate in rate_rows), Decimal(0))
        weighted_rate = (sum((row["_balance"] * rate for row, rate in rate_rows), Decimal(0)) / rate_balance
                         if rate_balance > 0 else None)
        raw_statuses = defaultdict(lambda: {"loan_count": 0, "balance": Decimal(0)})
        for row in selected:
            status = row.get("default_status")
            label = json.dumps(status, sort_keys=True) if isinstance(status, (str, bool, int, float)) else "UNKNOWN"
            raw_statuses[label]["loan_count"] += 1
            raw_statuses[label]["balance"] += row["_balance"]
        by_currency[currency] = {
            "loan_count": len(selected), "observed_balance": amount(total),
            "weighted_interest_rate_decimal": amount(weighted_rate),
            "interest_rate_unit": "decimal" if interest_rate_unit in ("decimal", "percent") else None,
            "interest_rate_not_computed_reason": None if interest_rate_unit in ("decimal", "percent") else "Source rate units are not declared in the mapping",
            "interest_rate_known_positive_balance": amount(rate_balance),
            "interest_rate_known_loan_count": len(rate_rows),
            "principal_arrears": arrears_summary(selected, "principal_arrears_days", "principal_arrears_amount", total),
            "interest_arrears": arrears_summary(selected, "interest_arrears_days", "interest_arrears_amount", total),
            "default_flag": {
                "true_balance": amount(default_true), "known_boolean_status_balance": amount(known_default_balance),
                "true_share_of_known_status_balance": ratio(default_true, known_default_balance),
                "known_boolean_status_loan_count": len(known_default),
                "unknown_or_nonboolean_status_loan_count": len(selected) - len(known_default),
                "raw_status_distribution": {key: {"loan_count": item["loan_count"], "balance": amount(item["balance"])}
                                            for key, item in sorted(raw_statuses.items())}},
            "concentrations": {field: group_balances(selected, field, total)
                               for field in ("deal_id", "pool", "originator", "servicer", "country", "sector", "vintage", "obligor_id")}}
    return {"as_of": target, "loan_count": len(rows), "deal_count": len(observed_deals),
            "pool_count": len({row["pool"] for row in rows}),
            "missing_performance_loan_count": missing_performance, "by_currency": by_currency}, rows


def analyse_portfolio(snapshot, holdings_rows, mapping, as_of, compare_as_of=None):
    """Return an observed-universe report; incomplete retrieval is never a portfolio total."""
    if not isinstance(snapshot, dict) or not isinstance(mapping, dict):
        raise AnalysisError("Snapshot and mapping must be JSON objects")
    if not isinstance(snapshot.get("source", {}), dict) or not isinstance(snapshot.get("coverage", {}), dict):
        raise AnalysisError("Source and coverage metadata must be objects")
    if not isinstance(snapshot.get("tables", {}), dict) or not isinstance(mapping.get("tables", {}), dict):
        raise AnalysisError("Tables and field mappings must be objects")
    if not isinstance(as_of, str) or len(as_of) != 10 or cutoff(as_of) != as_of:
        raise AnalysisError("as_of must be an ISO date YYYY-MM-DD")
    if compare_as_of is not None and (not isinstance(compare_as_of, str) or len(compare_as_of) != 10
                                      or cutoff(compare_as_of) != compare_as_of or compare_as_of >= as_of):
        raise AnalysisError("compare_as_of must be an earlier ISO date")
    if (type(snapshot.get("schema_version")) is not int or snapshot.get("schema_version") != 1
            or type(mapping.get("schema_version")) is not int or mapping.get("schema_version") != 1):
        raise AnalysisError("Snapshot and mapping schema_version must be 1")
    tables = {table: mapped_table(snapshot, mapping, table)
              for table in ("deals", "bond_tranches", "loans", "performances", "obligors")}
    for required in ("loans", "bond_tranches"):
        for logical in ("deal_id", "report_date"):
            if not mapping.get("tables", {}).get(required, {}).get(logical):
                raise AnalysisError(f"Mapping {required}.{logical} is required")
    if not mapping.get("tables", {}).get("loans", {}).get("balance"):
        raise AnalysisError("Mapping loans.balance is required")
    exceptions = []
    input_complete = snapshot.get("coverage", {}).get("complete") is True
    table_coverage = snapshot.get("coverage", {}).get("tables", {})
    if not isinstance(table_coverage, dict):
        raise AnalysisError("Per-table coverage must be an object")
    if snapshot.get("source", {}).get("kind") == "deeploans-api":
        input_complete = False
    table_reconciliation = {}
    for table, rows in snapshot.get("tables", {}).items():
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise AnalysisError(f"Table {table} must be a list of objects")
        entry = table_coverage.get(table, {})
        declared = entry.get("row_count") if isinstance(entry, dict) else None
        count_valid = type(declared) is int and declared >= 0 and declared == len(rows)
        table_reconciliation[table] = {"declared_row_count": declared if type(declared) is int else None,
                                       "observed_row_count": len(rows), "row_count_reconciled": count_valid}
        if not count_valid:
            input_complete = False
            issue(exceptions, "table_row_count_unreconciled", "Declared row count is missing, invalid or differs from the actual rows", table)
    for table in ("loans", "bond_tranches", "performances", "obligors"):
        entry = table_coverage.get(table, {})
        if table not in snapshot.get("tables", {}) or not isinstance(entry, dict) or entry.get("complete") is not True:
            input_complete = False
            issue(exceptions, "table_completeness_unverified", "Completeness is unverified for a table used by the reported measures", table)
    if not input_complete:
        issue(exceptions, "input_completeness_unverified", "Input is incomplete or completeness is unverified; all amounts describe only observed records")
    current_tranches = rows_at_date(tables["bond_tranches"], as_of, "bond_tranches", exceptions)
    current_loans = rows_at_date(tables["loans"], as_of, "loans", exceptions)
    holdings, held_deals = _holdings(holdings_rows, current_tranches, current_loans, exceptions)
    interest_rate_unit = mapping.get("interest_rate_unit")
    current, current_rows = _collateral(tables, held_deals, as_of, exceptions, interest_rate_unit)
    comparison = None
    if compare_as_of:
        previous, previous_rows = _collateral(tables, held_deals, compare_as_of, exceptions, interest_rate_unit)
        current_index = {loan_key(row): row for row in current_rows}
        previous_index = {loan_key(row): row for row in previous_rows}
        common = set(current_index) & set(previous_index)
        movements = {}
        for currency in sorted(set(current["by_currency"]) | set(previous["by_currency"])):
            current_balance = number(current["by_currency"].get(currency, {}).get("observed_balance"))
            prior_balance = number(previous["by_currency"].get(currency, {}).get("observed_balance"))
            stable = [key for key in common if current_index[key]["currency"] == currency == previous_index[key]["currency"]]
            movements[currency] = {"current_observed_balance": amount(current_balance),
                                   "previous_observed_balance": amount(prior_balance),
                                   "observed_balance_change": amount(current_balance - prior_balance) if current_balance is not None and prior_balance is not None else None,
                                   "same_currency_matched_loan_count": len(stable),
                                   "same_currency_matched_balance_change": amount(sum(
                                       (current_index[key]["_balance"] - previous_index[key]["_balance"] for key in stable), Decimal(0))) if stable else None}
        comparison = {"previous_cutoff": previous, "matched_loan_count": len(common),
                      "new_observed_loan_count": len(set(current_index) - set(previous_index)),
                      "no_longer_observed_loan_count": len(set(previous_index) - set(current_index)),
                      "by_currency": movements,
                      "interpretation": "Observed movements include changes in coverage and loan population; they are not prepayment, default or loss rates."}
    mapped_positions = sum(position["mapping_status"] == "mapped" for position in holdings["positions"])
    metric_gaps = []
    for currency, bucket in holdings["by_currency"].items():
        if bucket["positions_missing_market_value"]:
            metric_gaps.append({"metric": "market_value", "currency": currency,
                                "missing_count": bucket["positions_missing_market_value"], "unit": "positions"})
        if any(position["currency"] == currency and position["market_value"] is not None for position in holdings["positions"]):
            metric_gaps.append({"metric": "market_value_date", "currency": currency,
                                "reason": "Supplied market values have no independently verified valuation date"})
    for currency, bucket in current["by_currency"].items():
        defaults = bucket["default_flag"]
        if defaults["unknown_or_nonboolean_status_loan_count"]:
            metric_gaps.append({"metric": "default_flag", "currency": currency,
                                "missing_count": defaults["unknown_or_nonboolean_status_loan_count"], "unit": "loans"})
        for arrears_type in ("principal_arrears", "interest_arrears"):
            arrears = bucket[arrears_type]
            unknown_days = arrears["buckets"].get("unknown", {}).get("loan_count", 0)
            if unknown_days:
                metric_gaps.append({"metric": arrears_type + "_days", "currency": currency,
                                    "missing_count": unknown_days, "unit": "loans"})
            if arrears["arrears_amount_missing_or_invalid_loan_count"]:
                metric_gaps.append({"metric": arrears_type + "_amount", "currency": currency,
                                    "missing_count": arrears["arrears_amount_missing_or_invalid_loan_count"], "unit": "loans"})
        for dimension, groups in bucket["concentrations"].items():
            if groups.get("UNKNOWN", {}).get("loan_count", 0):
                metric_gaps.append({"metric": dimension, "currency": currency,
                                    "missing_count": groups["UNKNOWN"]["loan_count"], "unit": "loans"})
    return {"schema_version": 1, "as_of": as_of, "source": source_provenance(snapshot.get("source", {})),
            "coverage": {"status": "complete_for_declared_input_scope" if input_complete and not exceptions else "partial_or_unverified",
                         "input_complete": input_complete, "input_position_count": len(holdings_rows),
                         "table_reconciliation": table_reconciliation,
                         "reported_position_count": len(holdings["positions"]), "mapped_position_count": mapped_positions,
                         "unmapped_position_count": len(holdings["positions"]) - mapped_positions,
                         "unique_held_deal_count": len(held_deals),
                         "scope_note": "Collateral is the observed pool backing mapped holdings, counted once per loan and cutoff. It is not owned collateral exposure."},
            "holdings": holdings, "collateral": current, "comparison": comparison, "exceptions": exceptions,
            "metric_coverage": {"status": "partial" if metric_gaps else "available_for_observed_records", "gaps": metric_gaps},
            "reference_data_notes": ["Holdings are matched by explicit deal and tranche/security identifiers against bond_tranches; the deals endpoint has no verified deal key in the current mapping.",
                                     "Underlying loans are grouped by deal and pool. A shared deal identifier does not establish a legal pool-to-tranche allocation or ownership percentage."],
            "calculation_boundaries": ["Currencies are reported separately; no implicit FX conversion.",
                                       "Position balances and market values are supplied inputs, not independently priced valuations.",
                                       "Supplied market values have no independently verified valuation date.",
                                       "No CPR/CDR, expected loss, tranche waterfall, OAS, duration or DV01 is inferred."]}


def markdown_report(report):
    lines = ["# ABS portfolio review", ""]
    if report.get("source", {}).get("kind") == "synthetic-fixture":
        lines += ["**Synthetic demonstration data - not a real portfolio.**", ""]
    lines += [f"Reporting date: {report['as_of']}",
             f"Dataset coverage: **{report['coverage']['status']}**",
             f"Metric availability: **{report['metric_coverage']['status']}**", "",
             report["coverage"]["scope_note"], "", "## Holdings", "",
             "| Currency | Supplied position balance | Known supplied market value | Positions missing market value |",
             "| --- | ---: | ---: | ---: |"]
    for currency, bucket in report["holdings"]["by_currency"].items():
        lines.append(f"| {currency} | {bucket['position_balance']} | {bucket['known_market_value']} | {bucket['positions_missing_market_value']} |")
    lines += ["", "## Observed collateral", "",
              "| Currency | Loans | Observed balance | Known principal arrears amount |",
              "| --- | ---: | ---: | ---: |"]
    for currency, bucket in report["collateral"]["by_currency"].items():
        lines.append(f"| {currency} | {bucket['loan_count']} | {bucket['observed_balance']} | {bucket['principal_arrears']['known_arrears_amount'] if bucket['principal_arrears']['known_arrears_amount'] is not None else 'Unavailable'} |")
    lines += ["", "These figures cover observed records only. Missing values are not zero.", "", "## Collateral findings", ""]
    for currency, bucket in report["collateral"]["by_currency"].items():
        defaults = bucket["default_flag"]
        lines.append(f"### {currency}")
        lines += ["", "Principal arrears are grouped by reported days past due; shares use this currency's observed collateral balance.", "",
                  "| Principal arrears bucket | Loans | Balance | Share of observed balance |",
                  "| --- | ---: | ---: | ---: |"]
        for key, item in bucket["principal_arrears"]["buckets"].items():
            lines.append(f"| {key} | {item['loan_count']} | {item['balance']} | {item['share_of_observed_balance'] if item['share_of_observed_balance'] is not None else 'Unavailable'} |")
        lines += ["", f"Transaction default flag: true balance {defaults['true_balance']}; known boolean-status balance {defaults['known_boolean_status_balance']}; unknown or nonboolean status on {defaults['unknown_or_nonboolean_status_loan_count']} loans.", ""]
        for dimension in ("deal_id", "originator", "servicer", "country", "sector", "vintage"):
            groups = bucket["concentrations"][dimension]
            if groups:
                top = sorted(groups.items(), key=lambda item: (-Decimal(item[1]["balance"]), item[0]))[:3]
                descriptions = [f"{markdown_text(key)}: {item['balance']}" for key, item in top]
                lines.append(f"- Largest observed {dimension} groups ({currency} balance): " + "; ".join(descriptions) + ".")
        lines.append("")
    if report["comparison"]:
        lines += ["", "## Observed movements", "", report["comparison"]["interpretation"], "",
                  "| Currency | Previous observed balance | Current observed balance | Change |",
                  "| --- | ---: | ---: | ---: |"]
        for currency, bucket in report["comparison"]["by_currency"].items():
            show = lambda value: value if value is not None else "Unavailable"
            lines.append(f"| {currency} | {show(bucket['previous_observed_balance'])} | {show(bucket['current_observed_balance'])} | {show(bucket['observed_balance_change'])} |")
    lines += ["", "## Metric coverage gaps", ""]
    if report["metric_coverage"]["gaps"]:
        for gap in report["metric_coverage"]["gaps"]:
            detail = gap.get("reason") or f"Missing or unclassified on {gap['missing_count']} {gap['unit']}"
            lines.append(f"- {gap['currency']} {gap['metric']}: {detail}.")
    else:
        lines.append("Reported measures are available for the observed records.")
    lines += ["", "## Exceptions and gaps", ""]
    if report["exceptions"]:
        lines += [f"- {item['code']}: {markdown_text(item['message'])}" + (f" ({item['count']} records)" if "count" in item else "") for item in report["exceptions"]]
    else:
        lines.append("No dataset or linkage exceptions detected within the declared input scope. Metric availability is reported separately above.")
    lines += ["", "## Calculation boundaries", ""] + ["- " + item for item in report["calculation_boundaries"]]
    lines += ["", "## Linkage notes", ""] + ["- " + item for item in report["reference_data_notes"]]
    lines += ["", "## Source provenance", ""]
    for key, value in report.get("source", {}).items():
        lines.append(f"- {key}: {markdown_text(value)}")
    if report.get("provenance"):
        lines += ["", "## Provenance", ""]
        for key, value in report["provenance"].items():
            lines.append(f"- {key}: {value}")
    return "\n".join(lines) + "\n"


def markdown_text(value):
    return str(value).replace("\n", " ").replace("\r", " ").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--holdings", required=True, type=Path)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--compare-as-of")
    parser.add_argument("--mapping", type=Path, default=Path(__file__).resolve().parents[1] / "references/sme-api-fields.json")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        reject_constant = lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON constants are invalid"))
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"), parse_constant=reject_constant)
        mapping = json.loads(args.mapping.read_text(encoding="utf-8"), parse_constant=reject_constant)
        with args.holdings.open(encoding="utf-8-sig", newline="") as stream:
            holdings = list(csv.DictReader(stream))
        report = analyse_portfolio(snapshot, holdings, mapping, args.as_of, args.compare_as_of)
        report["provenance"] = {"snapshot_sha256": sha256(args.snapshot), "holdings_sha256": sha256(args.holdings),
                                "mapping_sha256": sha256(args.mapping), "helper_sha256": sha256(__file__)}
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (args.output_dir / "report.md").write_text(markdown_report(report), encoding="utf-8")
    except (AnalysisError, OSError, ValueError, TypeError) as exc:
        print(f"Analysis failed: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote report.json and report.md; coverage={report['coverage']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
