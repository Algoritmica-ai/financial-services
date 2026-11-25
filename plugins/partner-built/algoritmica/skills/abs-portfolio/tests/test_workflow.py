"""Full synthetic SME HTTP retrieval -> real field map -> CLI report."""
import csv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit


SKILL = Path(__file__).resolve().parents[1]


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name, SKILL / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkflowTest(unittest.TestCase):
    def test_api_observations_produce_reconciled_provisional_report(self):
        api = load_helper('deeploans_api')
        tables = {name: [] for name in api.DEFAULT_TABLES}
        for day, eur_balances, usd_balance in (
            ('2026-09-30', [100, 200], 50),
            ('2026-10-31', [90, 150], 40),
        ):
            for deal, pool, loan, balance, currency, late in (
                ('D1', 'POOL1', 'L1', eur_balances[0], 'EUR', False),
                ('D1', 'POOL1', 'L2', eur_balances[1], 'EUR', True),
                ('D2', 'POOL2', 'L1', usd_balance, 'USD', False),
            ):
                key = {'dl_code': deal, 'AS2': pool, 'AS3': loan, 'AS1': day}
                tables['loans'].append({**key, 'AS55': balance, 'AS52': currency,
                                        'AS50': '2022-01-31', 'AS7': loan})
                tables['performances'].append({**key, 'AS118': 60 if late else 0,
                    'AS116': 0, 'AS117': 10 if late else 0, 'AS115': 0,
                    'AS121': late, 'AS122': False})
                tables['obligors'].append({**key, 'AS7': loan, 'AS15': 'IE', 'AS42': 'C'})
            for deal, tranche, security, currency in (
                ('D1', 'A', 'TEST-A', 'EUR'), ('D1', 'B', 'TEST-B', 'EUR'),
                ('D2', 'A', 'TEST-C', 'USD'),
            ):
                tables['bond_tranches'].append({'dl_code': deal, 'BS1': day,
                    'BS25': tranche, 'BS26': security, 'BS29': currency})

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                self.server.seen_paths.append(parsed.path)
                table = parsed.path.rsplit('/', 1)[-1]
                query = parse_qs(parsed.query)
                offset = int(query['offset'][0])
                # Deliberately clamp every request to one row.
                rows = tables[table][offset:offset + 1]
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(rows).encode('utf-8'))

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.seen_paths = []
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            client = api.DeeploansClient('http://127.0.0.1:' + str(server.server_port),
                                        'synthetic-test-key', page_size=1000)
            snapshot = client.fetch_snapshot()
        finally:
            server.shutdown()
            server.server_close()
            worker.join()
        self.assertTrue(snapshot['coverage']['pagination_exhausted'])
        self.assertFalse(snapshot['coverage']['complete'])
        self.assertTrue(all(path.startswith('/api/v1/sme/') for path in server.seen_paths))
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / 'snapshot.json'
            source.write_text(json.dumps(snapshot), encoding='utf-8')
            holdings = folder / 'holdings.csv'
            with holdings.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=['position_id', 'deal_id',
                    'tranche_id', 'security_id', 'position_balance', 'currency', 'market_value'])
                writer.writeheader()
                writer.writerows([
                    dict(position_id='P1', deal_id='D1', tranche_id='A', security_id='TEST-A',
                         position_balance='10', currency='EUR', market_value='9.8'),
                    dict(position_id='P2', deal_id='D1', tranche_id='B', security_id='TEST-B',
                         position_balance='5', currency='EUR', market_value=''),
                    dict(position_id='P3', deal_id='D2', tranche_id='A', security_id='TEST-C',
                         position_balance='2', currency='USD', market_value='2'),
                ])
            result = subprocess.run([sys.executable, str(SKILL / 'scripts/analyse_portfolio.py'),
                '--snapshot', str(source), '--holdings', str(holdings), '--as-of', '2026-10-31',
                '--compare-as-of', '2026-09-30', '--output-dir', str(folder / 'review')],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((folder / 'review/report.json').read_text())
            self.assertEqual(report['coverage']['status'], 'partial_or_unverified')
            self.assertEqual(report['coverage']['mapped_position_count'], 3)
            self.assertEqual(report['collateral']['pool_count'], 2)
            self.assertEqual(report['collateral']['by_currency']['EUR']['observed_balance'], '240')
            self.assertEqual(report['collateral']['by_currency']['USD']['observed_balance'], '40')
            self.assertEqual(report['holdings']['by_currency']['EUR']['position_balance'], '15')
            self.assertEqual(report['holdings']['by_currency']['EUR']['positions_missing_market_value'], 1)
            self.assertEqual(report['comparison']['by_currency']['EUR']['observed_balance_change'], '-60')
            self.assertEqual(report['collateral']['by_currency']['EUR']['default_flag']['true_balance'], '150')
            markdown = (folder / 'review/report.md').read_text()
            self.assertIn('partial_or_unverified', markdown)
            self.assertIn('snapshot_sha256', markdown)
            self.assertNotIn('synthetic-test-key', markdown)


if __name__ == '__main__':
    unittest.main()
