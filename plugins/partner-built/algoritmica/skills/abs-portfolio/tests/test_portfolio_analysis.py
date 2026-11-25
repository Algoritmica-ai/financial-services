"""Financial invariants verified from small independently counted records."""
import copy
import csv
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("analyse_portfolio", BASE / "scripts/analyse_portfolio.py")
analytics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analytics)


class PortfolioAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = json.loads((BASE / "tests/fixtures/sme-snapshot.json").read_text())
        self.mapping = json.loads((BASE / "references/sme-api-fields.json").read_text())
        with (BASE / "tests/fixtures/holdings.csv").open(newline="") as stream:
            self.holdings = list(csv.DictReader(stream))

    def analyse(self, **kwargs):
        return analytics.analyse_portfolio(self.snapshot, self.holdings, self.mapping,
                                           kwargs.pop("as_of", "2026-11-30"), **kwargs)

    def test_shared_pool_counted_once_and_positions_not_collateral_ownership(self):
        report = self.analyse()
        self.assertEqual(report["coverage"]["mapped_position_count"], 3)
        self.assertEqual(report["collateral"]["pool_count"], 2)
        self.assertEqual(report["collateral"]["by_currency"]["EUR"]["observed_balance"], "100")
        self.assertEqual(report["holdings"]["by_currency"]["EUR"]["position_balance"], "50")

    def test_currencies_are_not_added_and_loan_id_is_deal_scoped(self):
        report = self.analyse()
        self.assertEqual(set(report["collateral"]["by_currency"]), {"EUR", "USD"})
        self.assertEqual(report["collateral"]["loan_count"], 3)
        self.assertEqual(report["collateral"]["by_currency"]["USD"]["observed_balance"], "50")
        self.assertNotIn("total_balance", report["collateral"])

    def test_arrears_buckets_use_balance_and_explicit_denominator(self):
        bucket = self.analyse()["collateral"]["by_currency"]["EUR"]
        self.assertEqual(bucket["principal_arrears"]["buckets"]["31_60"]["balance"], "20")
        self.assertEqual(Decimal(bucket["principal_arrears"]["buckets"]["31_60"]["share_of_observed_balance"]), Decimal("0.2"))
        self.assertEqual(bucket["principal_arrears"]["known_arrears_amount"], "5")
        self.assertEqual(bucket["default_flag"]["true_balance"], "20")
        self.assertEqual(Decimal(bucket["default_flag"]["true_share_of_known_status_balance"]), Decimal("0.2"))

    def test_missing_status_and_amount_are_unknown_not_current_or_zero(self):
        usd = self.analyse()["collateral"]["by_currency"]["USD"]
        self.assertEqual(usd["principal_arrears"]["buckets"]["unknown"]["balance"], "50")
        self.assertIsNone(usd["principal_arrears"]["known_arrears_amount"])
        self.assertEqual(usd["default_flag"]["unknown_or_nonboolean_status_loan_count"], 1)
        self.assertIsNone(usd["default_flag"]["true_share_of_known_status_balance"])

    def test_duplicate_loan_key_quarantines_all_occurrences(self):
        self.snapshot["tables"]["loans"].append(copy.deepcopy(self.snapshot["tables"]["loans"][3]))
        report = self.analyse()
        self.assertEqual(report["collateral"]["by_currency"]["EUR"]["observed_balance"], "20")
        self.assertTrue(any(item["code"] == "duplicate_key" for item in report["exceptions"]))
        self.assertEqual(report["coverage"]["status"], "partial_or_unverified")

    def test_duplicate_performance_does_not_choose_last_status(self):
        duplicate = copy.deepcopy(self.snapshot["tables"]["performances"][4])
        duplicate["AS121"] = False
        self.snapshot["tables"]["performances"].append(duplicate)
        eur = self.analyse()["collateral"]["by_currency"]["EUR"]
        self.assertEqual(eur["default_flag"]["true_balance"], "0")
        self.assertEqual(eur["default_flag"]["unknown_or_nonboolean_status_loan_count"], 1)

    def test_unknown_holding_is_reported_and_its_pool_not_invented(self):
        self.holdings[2]["deal_id"] = "NOT-IN-SOURCE"
        report = self.analyse()
        self.assertEqual(report["coverage"]["mapped_position_count"], 2)
        self.assertEqual(report["coverage"]["unmapped_position_count"], 1)
        self.assertEqual(report["collateral"]["deal_count"], 1)
        self.assertNotIn("USD", report["collateral"]["by_currency"])

    def test_incomplete_api_snapshot_is_explicitly_provisional(self):
        self.snapshot["coverage"]["complete"] = False
        self.snapshot["source"] = {"kind": "deeploans-api", "ordered_snapshot": False}
        report = self.analyse()
        self.assertFalse(report["coverage"]["input_complete"])
        self.assertEqual(report["coverage"]["status"], "partial_or_unverified")
        self.assertIn("observed records", analytics.markdown_report(report))

    def test_api_metadata_cannot_be_promoted_to_complete_by_one_flag(self):
        self.snapshot["coverage"]["complete"] = True
        self.snapshot["source"] = {"kind": "deeploans-api", "ordered_snapshot": False}
        report = self.analyse()
        self.assertFalse(report["coverage"]["input_complete"])
        self.assertEqual(report["coverage"]["status"], "partial_or_unverified")

    def test_declared_row_count_detects_missing_observation(self):
        del self.snapshot["tables"]["loans"][4]
        report = self.analyse()
        self.assertEqual(report["collateral"]["by_currency"]["EUR"]["observed_balance"], "80")
        self.assertFalse(report["coverage"]["input_complete"])
        self.assertEqual(report["coverage"]["status"], "partial_or_unverified")
        self.assertFalse(report["coverage"]["table_reconciliation"]["loans"]["row_count_reconciled"])
        self.assertTrue(any(item["code"] == "table_row_count_unreconciled" for item in report["exceptions"]))

    def test_manifest_row_count_must_be_a_nonnegative_integer(self):
        for declared in (True, -1, "6", None):
            with self.subTest(declared=declared):
                self.snapshot["coverage"]["tables"]["loans"]["row_count"] = declared
                self.assertFalse(self.analyse()["coverage"]["input_complete"])

    def test_optional_obligor_completeness_affects_reported_classifications(self):
        self.snapshot["tables"]["obligors"] = []
        self.snapshot["coverage"]["tables"]["obligors"].update(complete=False, row_count=0)
        report = self.analyse()
        self.assertFalse(report["coverage"]["input_complete"])
        self.assertEqual(report["coverage"]["status"], "partial_or_unverified")
        self.assertTrue(any(gap["metric"] == "country" for gap in report["metric_coverage"]["gaps"]))

    def test_unused_deals_metadata_does_not_require_completeness(self):
        self.snapshot["coverage"]["tables"]["deals"]["complete"] = False
        self.assertTrue(self.analyse()["coverage"]["input_complete"])

    def test_synthetic_source_is_prominent_and_provenance_is_whitelisted(self):
        self.snapshot["source"].update(api_key="SECRET", base_url="https://user:SECRET@example.test/api?key=SECRET#SECRET")
        report = self.analyse()
        markdown = analytics.markdown_report(report)
        self.assertIn("Synthetic demonstration data - not a real portfolio", markdown)
        self.assertLess(markdown.index("Synthetic demonstration data"), markdown.index("Reporting date"))
        self.assertIn("synthetic-fixture", markdown)
        self.assertIn("fetched_at", markdown)
        self.assertEqual(report["source"]["base_url"], "https://example.test/api")
        self.assertNotIn("SECRET", json.dumps(report))

    def test_metric_gaps_are_separate_from_dataset_completeness(self):
        report = self.analyse()
        self.assertTrue(report["coverage"]["input_complete"])
        self.assertEqual(report["metric_coverage"]["status"], "partial")
        gaps = {gap["metric"] for gap in report["metric_coverage"]["gaps"]}
        self.assertTrue({"market_value", "market_value_date", "default_flag"} <= gaps)
        self.assertIn("## Metric coverage gaps", analytics.markdown_report(report))
        self.assertNotIn("No input exceptions", analytics.markdown_report(report))

    def test_missing_market_value_is_counted_without_fabrication(self):
        eur = self.analyse()["holdings"]["by_currency"]["EUR"]
        self.assertEqual(eur["known_market_value"], "39")
        self.assertEqual(eur["positions_missing_market_value"], 1)

    def test_exact_cutoff_required_no_silent_carry_forward(self):
        report = self.analyse(as_of="2026-12-31")
        self.assertEqual(report["coverage"]["mapped_position_count"], 0)
        self.assertEqual(report["collateral"]["loan_count"], 0)

    def test_malformed_iso_timestamp_is_not_truncated_to_a_valid_date(self):
        self.snapshot["tables"]["loans"][3]["AS1"] = "2026-11-30Tnot-an-iso-timestamp"
        report = self.analyse()
        self.assertEqual(report["collateral"]["by_currency"]["EUR"]["observed_balance"], "20")
        self.assertEqual(report["coverage"]["status"], "partial_or_unverified")
        self.assertIsNone(analytics.cutoff("2026-11-30Tnot-an-iso-timestamp"))
        self.assertEqual(analytics.cutoff("2026-11-30T00:00:00Z"), "2026-11-30")

    def test_comparison_uses_unique_observed_loans_not_inferred_prepayment(self):
        report = self.analyse(compare_as_of="2026-10-31")
        self.assertEqual(report["comparison"]["matched_loan_count"], 3)
        self.assertEqual(report["comparison"]["by_currency"]["EUR"]["observed_balance_change"], "-20")
        self.assertEqual(report["comparison"]["by_currency"]["USD"]["observed_balance_change"], "-10")
        self.assertNotIn("CPR", report["comparison"])

    def test_absent_comparison_cutoff_does_not_become_zero(self):
        report = self.analyse(compare_as_of="2026-09-30")
        eur = report["comparison"]["by_currency"]["EUR"]
        self.assertIsNone(eur["previous_observed_balance"])
        self.assertIsNone(eur["observed_balance_change"])
        self.assertIsNone(eur["same_currency_matched_balance_change"])
        self.assertIn("Unavailable", analytics.markdown_report(report))

    def test_currency_mismatch_blocks_mapping(self):
        self.holdings[2]["currency"] = "EUR"
        report = self.analyse()
        self.assertEqual(report["coverage"]["mapped_position_count"], 2)
        self.assertEqual(report["holdings"]["positions"][2]["reason"], "position_tranche_currency_mismatch")
        self.assertNotIn("USD", report["collateral"]["by_currency"])

    def test_malformed_identifiers_and_currencies_are_not_invented_labels(self):
        self.holdings[0]["deal_id"] = {"fake": "D1"}
        self.holdings[2]["currency"] = "USD|\n# spoof"
        report = self.analyse()
        self.assertEqual(report["coverage"]["mapped_position_count"], 1)
        self.assertNotIn("spoof", analytics.markdown_report(report))

    def test_conflicting_obligor_identity_is_not_silently_overwritten(self):
        self.snapshot["tables"]["performances"][3]["AS7"] = "WRONG-OBLIGOR"
        report = self.analyse()
        self.assertTrue(any(item["code"] == "conflicting_supplemental_field" for item in report["exceptions"]))
        groups = report["collateral"]["by_currency"]["EUR"]["concentrations"]["obligor_id"]
        self.assertIn("O1", groups)
        self.assertNotIn("WRONG-OBLIGOR", groups)

    def test_invalid_balance_is_excluded_not_coerced_to_zero(self):
        self.snapshot["tables"]["loans"][3]["AS55"] = "not-a-number"
        report = self.analyse()
        self.assertEqual(report["collateral"]["by_currency"]["EUR"]["observed_balance"], "20")
        self.assertTrue(any(item["code"] == "invalid_collateral_balance_or_currency" for item in report["exceptions"]))

    def test_rates_require_declared_units(self):
        self.mapping["tables"]["loans"]["interest_rate"] = "test_rate"
        for row in self.snapshot["tables"]["loans"]:
            row["test_rate"] = "5"
        eur = self.analyse()["collateral"]["by_currency"]["EUR"]
        self.assertIsNone(eur["weighted_interest_rate_decimal"])
        self.mapping["interest_rate_unit"] = "percent"
        eur = self.analyse()["collateral"]["by_currency"]["EUR"]
        self.assertEqual(Decimal(eur["weighted_interest_rate_decimal"]), Decimal("0.05"))

    def test_empty_holdings_and_invalid_date_fail(self):
        with self.assertRaises(analytics.AnalysisError):
            analytics.analyse_portfolio(self.snapshot, [], self.mapping, "2026-11-30")
        with self.assertRaises(analytics.AnalysisError):
            self.analyse(as_of="30/11/2026")
        with self.assertRaises(analytics.AnalysisError):
            self.analyse(as_of=None)

    def test_cli_runs_offline_with_checksums_and_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(BASE / "scripts/analyse_portfolio.py"),
                                     "--snapshot", str(BASE / "tests/fixtures/sme-snapshot.json"),
                                     "--holdings", str(BASE / "tests/fixtures/holdings.csv"),
                                     "--as-of", "2026-11-30", "--compare-as-of", "2026-10-31",
                                     "--output-dir", directory], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads((Path(directory) / "report.json").read_text())
            self.assertEqual(len(output["provenance"]["snapshot_sha256"]), 64)
            self.assertTrue((Path(directory) / "report.md").is_file())


if __name__ == "__main__":
    unittest.main()
