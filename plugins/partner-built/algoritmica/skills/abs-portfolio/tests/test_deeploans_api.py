"""Exercise the real HTTP client against synthetic loopback responses only."""

from contextlib import contextmanager, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("deeploans_api_under_test", SCRIPTS / "deeploans_api.py")
api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(api)


@contextmanager
def server(*, rows=None, mode="normal", clamp=None):
    observations = []
    rows = rows if rows is not None else [{"AS3": "L1"}, {"AS3": "L2"}, {"AS3": "L3"}]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            observations.append({"path": parsed.path, "query": query,
                                 "key": self.headers.get("x-algoritmica-api-key")})
            if mode == "error":
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b'{"detail":"reflected-secret and confidential data"}')
                return
            if mode == "redirect":
                self.send_response(302)
                self.send_header("Location", "/capture-credentials")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if mode == "malformed":
                self.wfile.write(b"not JSON: reflected-secret")
                return
            if mode == "envelope":
                result = {"data": rows}
            elif mode == "arrays":
                result = [[1, 2]]
            elif mode == "nan":
                self.wfile.write(b'[{"balance":NaN}]')
                return
            else:
                limit = int(query["limit"][0])
                if clamp:
                    limit = min(limit, clamp)
                offset = int(query["offset"][0])
                result = rows[:limit] if mode == "repeat" else rows[offset:offset + limit]
                if mode == "ignore_limit":
                    result = rows
            self.wfile.write(json.dumps(result).encode())

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(httpd.server_port), observations
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join()


class UrlAndScopeTests(unittest.TestCase):
    def test_https_and_loopback_are_allowed(self):
        self.assertEqual(api.validate_base_url("https://example.invalid/mount/"), "https://example.invalid/mount")
        self.assertEqual(api.validate_base_url("http://127.0.0.1:9000"), "http://127.0.0.1:9000")
        self.assertEqual(api.validate_base_url("http://[::1]:9000"), "http://[::1]:9000")

    def test_unsafe_urls_are_rejected_without_reflecting_secrets(self):
        values = [None, "http://example.invalid", "https://user:secret@example.invalid",
                  "https://example.invalid?token=secret", "https://example.invalid/#secret",
                  "https://example.invalid/a/../b", "file:///tmp/test",
                  "https://example.invalid/api/v1/sme", "https://example.invalid:bad"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(api.ApiError) as error:
                api.validate_base_url(value)
            self.assertNotIn("secret", str(error.exception))

    def test_non_sme_tables_never_make_a_request(self):
        with server() as (url, requests):
            client = api.DeeploansClient(url, "test-key")
            with self.assertRaises(api.ApiError):
                client.fetch_table("../dev/users")
            with self.assertRaises(api.ApiError):
                client.fetch_snapshot(["mortgages"])
            self.assertEqual(requests, [])


class PaginationTests(unittest.TestCase):
    def test_server_clamp_does_not_make_short_page_complete(self):
        with server(clamp=1) as (url, requests):
            rows, state = api.DeeploansClient(url, "test-key", page_size=2).fetch_table("loans")
        self.assertEqual([r["AS3"] for r in rows], ["L1", "L2", "L3"])
        self.assertEqual([r["query"]["offset"][0] for r in requests], ["0", "1", "2", "3"])
        self.assertTrue(state["pagination_exhausted"])
        self.assertFalse(state["complete"])
        self.assertFalse(state["completeness_verified"])
        self.assertEqual(state["reason"], "unstable_offset_pagination")

    def test_repeated_page_stops_without_duplicating_it(self):
        with server(mode="repeat") as (url, _):
            rows, state = api.DeeploansClient(url, "test-key", page_size=2).fetch_table("loans")
        self.assertEqual(len(rows), 2)
        self.assertEqual(state["reason"], "repeated_page")
        self.assertFalse(state["pagination_exhausted"])

    def test_row_cap_records_truncation(self):
        with server() as (url, requests):
            rows, state = api.DeeploansClient(url, "test-key", page_size=2, max_rows=2).fetch_table("loans")
        self.assertEqual(len(rows), 2)
        self.assertEqual(state["reason"], "row_limit")
        self.assertFalse(state["pagination_exhausted"])
        self.assertEqual(requests[-1]["query"]["limit"], ["1"])

    def test_exact_cap_can_still_observe_final_empty_page(self):
        with server(rows=[{"id": 1}, {"id": 2}]) as (url, _):
            rows, state = api.DeeploansClient(url, "test-key", page_size=2, max_rows=2).fetch_table("loans")
        self.assertEqual(len(rows), 2)
        self.assertTrue(state["pagination_exhausted"])
        self.assertFalse(state["complete"])

    def test_page_cap_is_not_exhaustion(self):
        with server() as (url, _):
            rows, state = api.DeeploansClient(url, "test-key", page_size=2, max_pages=1).fetch_table("loans")
        self.assertEqual(len(rows), 2)
        self.assertEqual(state["reason"], "page_limit")
        self.assertFalse(state["pagination_exhausted"])

    def test_queries_are_encoded_and_offsets_remain_under_client_control(self):
        with server(rows=[]) as (url, requests):
            api.DeeploansClient(url, "test-key").fetch_table("loans", filter_query="dl_code:X AND AS1:2026-10-31", columns=["AS1", "AS3"])
        self.assertEqual(requests[0]["path"], "/api/v1/sme/loans")
        self.assertEqual(requests[0]["query"]["detailed"], ["true"])
        self.assertEqual(requests[0]["query"]["filter"], ["dl_code:X AND AS1:2026-10-31"])
        self.assertEqual(requests[0]["query"]["columns"], ["AS1,AS3"])


class ResponseAndCredentialTests(unittest.TestCase):
    def test_unexpected_response_shapes_and_nan_are_rejected(self):
        for mode, reason in [("envelope", "invalid_shape"), ("arrays", "invalid_shape"),
                             ("malformed", "invalid_json"), ("nan", "invalid_json"),
                             ("ignore_limit", "ignored_limit")]:
            with self.subTest(mode=mode), server(mode=mode) as (url, _):
                rows, state = api.DeeploansClient(url, "reflected-secret", page_size=1).fetch_table("loans")
                self.assertEqual(rows, [])
                self.assertEqual(state["reason"], reason)
                self.assertNotIn("reflected-secret", json.dumps(state))

    def test_redirect_is_not_followed(self):
        with server(mode="redirect") as (url, requests):
            rows, state = api.DeeploansClient(url, "test-key").fetch_table("loans")
        self.assertEqual(rows, [])
        self.assertEqual(state["reason"], "redirect_blocked")
        self.assertEqual(len(requests), 1)
        self.assertNotIn("capture-credentials", requests[0]["path"])

    def test_error_bodies_are_not_exposed_and_later_tables_are_skipped(self):
        with server(mode="error") as (url, requests):
            snapshot = api.DeeploansClient(url, "reflected-secret").fetch_snapshot(["loans", "performances"])
        self.assertEqual(len(requests), 1)
        self.assertNotIn("reflected-secret", json.dumps(snapshot))
        self.assertNotIn("confidential data", json.dumps(snapshot))
        self.assertEqual(snapshot["coverage"]["tables"]["performances"]["reason"], "not_requested_after_error")

    def test_response_byte_cap(self):
        with server() as (url, _), patch.object(api, "MAX_RESPONSE_BYTES", 10):
            _, state = api.DeeploansClient(url, "test-key").fetch_table("loans")
        self.assertEqual(state["reason"], "response_too_large")

    def test_snapshot_is_always_provisional(self):
        with server(rows=[]) as (url, _):
            snapshot = api.DeeploansClient(url, "test-key").fetch_snapshot(["loans", "performances"])
        self.assertTrue(snapshot["coverage"]["pagination_exhausted"])
        self.assertFalse(snapshot["coverage"]["complete"])
        self.assertFalse(snapshot["coverage"]["completeness_verified"])
        self.assertFalse(snapshot["source"]["ordered_snapshot"])
        self.assertNotIn("test-key", json.dumps(snapshot))

    def test_cli_saves_provisional_snapshot_and_returns_three(self):
        with server(rows=[]) as (url, _), tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "snapshot.json"
            stderr = io.StringIO()
            with patch.dict(os.environ, {"DEEPLOANS_API_URL": url, "DEEPLOANS_API_KEY": "test-key"}), redirect_stderr(stderr):
                code = api.main(["--tables", "loans", "--output", str(output)])
            self.assertEqual(code, 3)
            saved = json.loads(output.read_text())
            self.assertFalse(saved["coverage"]["complete"])
            self.assertNotIn("test-key", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
