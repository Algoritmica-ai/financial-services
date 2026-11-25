#!/usr/bin/env python3
"""Read-only SME API retrieval with explicit, conservative coverage metadata.

The current Deeploans backend paginates unordered SQL queries. Reaching an empty
offset page is recorded as exhaustion, never as verified portfolio completeness.
Only the Python standard library is required.
"""

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


TABLES = (
    "amortisation_profiles", "bond_collaterals", "bond_info", "bond_tranches",
    "deals", "financials", "interests", "loan_collaterals", "loans",
    "nace_codes", "nuts3_codes", "obligors", "performances",
)
DEFAULT_TABLES = ("deals", "bond_tranches", "loans", "performances", "obligors")
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
UNORDERED_REASON = (
    "The documented backend uses LIMIT/OFFSET without ORDER BY, a cursor, "
    "or a snapshot/count contract. Exhausting offsets does not verify completeness."
)


class ApiError(Exception):
    """A public error whose message never contains response bodies or credentials."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_base_url(value):
    """Accept a deployment root (optionally mounted under a path), not an endpoint."""
    if not value:
        raise ApiError("configuration", "Set DEEPLOANS_API_URL to the deployment root.")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (ValueError, TypeError):
        raise ApiError("configuration", "The API deployment URL is invalid.") from None
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ApiError("configuration", "The API URL must not contain credentials, a query, or a fragment.")
    host = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ApiError("configuration", "Use HTTPS, or HTTP for a loopback test server only.")
    segments = urllib.parse.unquote(parsed.path).split("/")
    if any(part in (".", "..") for part in segments):
        raise ApiError("configuration", "The API mount path contains an invalid segment.")
    if parsed.path.rstrip("/").endswith("/api/v1/sme"):
        raise ApiError("configuration", "Use the deployment root, excluding /api/v1/sme.")
    return value.rstrip("/")


def _digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")).hexdigest()


class DeeploansClient:
    def __init__(self, base_url, api_key, *, page_size=1000, max_rows=100000,
                 max_pages=1000, timeout=30, opener=None):
        self.base_url = validate_base_url(base_url)
        if not isinstance(api_key, str) or not api_key.strip() or any(c in api_key for c in "\r\n"):
            raise ApiError("configuration", "Set DEEPLOANS_API_KEY to a valid API credential.")
        if not 1 <= page_size <= 10000 or max_rows < 1 or max_pages < 1 or timeout <= 0:
            raise ApiError("configuration", "Page size, row/page limits and timeout must be positive and bounded.")
        self._api_key = api_key
        self.page_size = page_size
        self.max_rows = max_rows
        self.max_pages = max_pages
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener(NoRedirect())

    def _page(self, table, offset, limit, filter_query=None, columns=None):
        if table not in TABLES:
            raise ApiError("configuration", "Only documented SME table endpoints are supported.")
        params = {"detailed": "true", "limit": limit, "offset": offset}
        if filter_query:
            params["filter"] = filter_query
        if columns:
            params["columns"] = ",".join(columns)
        url = self.base_url + "/api/v1/sme/" + table + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={
            "Accept": "application/json", "x-algoritmica-api-key": self._api_key,
        }, method="GET")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            code = "redirect_blocked" if 300 <= error.code < 400 else "http_error"
            # Deliberately omit URLs, reason strings and response bodies: servers
            # can reflect credentials or confidential loan attributes into them.
            raise ApiError(code, "SME API request failed with HTTP status " + str(error.code) + ".") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise ApiError("network_error", "SME API request failed; check connectivity and configuration.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ApiError("response_too_large", "SME API response exceeded the configured byte limit.")
        try:
            rows = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            raise ApiError("invalid_json", "SME API returned invalid JSON.") from None
        # detailed=true is a list of row dictionaries in the source route; there
        # is no documented data/results envelope or total-count field.
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise ApiError("invalid_shape", "SME API detailed responses must be arrays of row objects.")
        if len(rows) > limit:
            raise ApiError("ignored_limit", "SME API returned more rows than requested.")
        return rows

    def fetch_table(self, table, *, filter_query=None, columns=None):
        if table not in TABLES:
            raise ApiError("configuration", "Only documented SME table endpoints are supported.")
        if columns and (not all(isinstance(c, str) and c.replace("_", "").isalnum() for c in columns)):
            raise ApiError("configuration", "Column names must contain letters, digits or underscores.")
        rows, fingerprints, requests = [], set(), 0
        state = {"complete": False, "completeness_verified": False,
                 "pagination_exhausted": False, "row_count": 0, "pages": 0,
                 "reason": "not_started", "consistency_note": UNORDERED_REASON}
        if filter_query:
            state["filter"] = filter_query
        if columns:
            state["columns"] = columns
        while requests < self.max_pages:
            remaining = self.max_rows - len(rows)
            # When exactly at the row cap, probe for an empty page without
            # retaining additional records. A nonempty response means truncated.
            requested = min(self.page_size, remaining) if remaining else 1
            try:
                page = self._page(table, len(rows), requested, filter_query, columns)
            except ApiError as error:
                state.update(reason=error.code, error=str(error))
                break
            requests += 1
            if not page:
                state.update(pagination_exhausted=True, reason="unstable_offset_pagination")
                break
            if not remaining:
                state["reason"] = "row_limit"
                break
            fingerprint = _digest(page)
            if fingerprint in fingerprints:
                state["reason"] = "repeated_page"
                break
            fingerprints.add(fingerprint)
            rows.extend(page)
            # A short page can result from the server clamping LIMIT. Keep going
            # until empty, with offset based on observed rows rather than LIMIT.
        else:
            state["reason"] = "page_limit"
        state.update(row_count=len(rows), pages=requests, sha256=_digest(rows))
        return rows, state

    def fetch_snapshot(self, tables=DEFAULT_TABLES, *, filters=None, columns=None):
        selected = list(dict.fromkeys(tables))
        if not selected or any(table not in TABLES for table in selected):
            raise ApiError("configuration", "Choose one or more documented SME tables.")
        filters, columns = filters or {}, columns or {}
        if any(table not in selected for table in set(filters) | set(columns)):
            raise ApiError("configuration", "Filters and columns must refer to selected tables.")
        result = {
            "schema_version": 1,
            "source": {"kind": "deeploans-api", "base_url": self.base_url,
                       "asset_class": "sme", "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                       "method": "offset_until_empty", "ordered_snapshot": False},
            "tables": {},
            "coverage": {"complete": False, "completeness_verified": False,
                         "pagination_exhausted": False, "tables": {},
                         "reason": "unstable_offset_pagination", "consistency_note": UNORDERED_REASON},
        }
        for table in selected:
            rows, state = self.fetch_table(table, filter_query=filters.get(table), columns=columns.get(table))
            result["tables"][table] = rows
            result["coverage"]["tables"][table] = state
            if state["reason"] in ("http_error", "redirect_blocked", "network_error"):
                for skipped in selected[selected.index(table) + 1:]:
                    result["tables"][skipped] = []
                    result["coverage"]["tables"][skipped] = {
                        "complete": False, "completeness_verified": False,
                        "pagination_exhausted": False, "row_count": 0, "pages": 0,
                        "reason": "not_requested_after_error"}
                break
        result["coverage"]["pagination_exhausted"] = all(
            state["pagination_exhausted"] for state in result["coverage"]["tables"].values())
        return result


def _table_options(values, label):
    result = {}
    for value in values or []:
        table, sep, argument = value.partition("=")
        if table not in TABLES or not sep or not argument or table in result:
            raise ApiError("configuration", "Use one " + label + " TABLE=VALUE entry per selected SME table.")
        result[table] = argument
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="Deployment root; otherwise DEEPLOANS_API_URL")
    parser.add_argument("--tables", nargs="+", choices=TABLES, default=list(DEFAULT_TABLES))
    parser.add_argument("--filter", action="append", default=[], metavar="TABLE=EXPR")
    parser.add_argument("--columns", action="append", default=[], metavar="TABLE=COLUMN,...")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-rows", type=int, default=100000, help="Safety cap per table")
    parser.add_argument("--max-pages", type=int, default=1000, help="Safety cap per table, including final probe")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        filters = _table_options(args.filter, "--filter")
        columns = {table: value.split(",") for table, value in _table_options(args.columns, "--columns").items()}
        client = DeeploansClient(args.base_url or os.environ.get("DEEPLOANS_API_URL"),
                                 os.environ.get("DEEPLOANS_API_KEY"), page_size=args.page_size,
                                 max_rows=args.max_rows, max_pages=args.max_pages, timeout=args.timeout)
        snapshot = client.fetch_snapshot(args.tables, filters=filters, columns=columns)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(snapshot, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except ApiError as error:
        print("API retrieval failed (" + error.code + "): " + str(error), file=sys.stderr)
        return 2
    except OSError:
        print("Could not write the snapshot output file.", file=sys.stderr)
        return 2
    exhausted = snapshot["coverage"]["pagination_exhausted"]
    print("Saved SME observations; portfolio completeness remains unverified.", file=sys.stderr)
    # Exit 3 is a deliberate provisional-result signal even after exhausted
    # pagination. Operational errors or truncation use 2; there is no false 0.
    return 3 if exhausted else 2


if __name__ == "__main__":
    sys.exit(main())
