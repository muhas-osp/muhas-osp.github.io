import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fg = load('fetch_grants', 'scripts/fetch_grants.py')
ic = load('import_catalogue', 'admin/import_catalogue.py')
TODAY = date(2026, 10, 27)
V = 'Verified - publish'


def rec(rid='1', **kw):
    """A REDCap label-export row with private sentinels in internal fields."""
    base = {
        'record_id': rid, 'funder_name': 'Wellcome', 'grant_title': 'Example <call>',
        'application_url': 'https://wellcome.org/x', 'osp_status': V,
        'tz_eligibility': 'Eligible', 'deadline_type': 'Fixed deadline(s)',
        'deadline_date': '2026-12-01', 'osp_notes': 'PRIVATE_SENTINEL_NOTES',
        'source_text': 'PRIVATE_SENTINEL_SOURCE', 'entry_method': 'Manual',
        'career_stage___1': 'Early-career', 'career_stage___2': '',
    }
    base.update(kw)
    return base


class PublishRules(unittest.TestCase):
    def ids(self, rows):
        return [g['id'] for g in fg.publishable_grants(rows, TODAY)]

    def test_only_verified_calls_publish(self):
        rows = [rec('1'), rec('2', osp_status='Pending OSP verification'),
                rec('3', osp_status='Rejected / withdrawn'), rec('4', osp_status='')]
        self.assertEqual(self.ids(rows), ['1'])

    def test_not_eligible_is_excluded_pending_is_shown(self):
        rows = [rec('1', tz_eligibility='Not eligible'), rec('2', tz_eligibility='Pending assessment')]
        self.assertEqual(self.ids(rows), ['2'])

    def test_internal_fields_never_published(self):
        out = json.dumps(fg.publishable_grants([rec()], TODAY))
        self.assertNotIn('PRIVATE_SENTINEL', out)
        for key in ('osp_notes', 'source_text', 'osp_status', 'entry_method'):
            self.assertNotIn(f'"{key}"', out)

    def test_closed_calls_drop_and_today_counts_as_open(self):
        rows = [rec('1', deadline_date='2026-10-26'), rec('2', deadline_date='2026-10-27')]
        self.assertEqual(self.ids(rows), ['2'])

    def test_multi_round_uses_next_future_round(self):
        g = fg.publishable_grants([rec(deadline_date='2026-06-01', deadline_date_2='2027-02-01',
                                       deadline_date_3='2026-11-15')], TODAY)[0]
        self.assertEqual(g['next_deadline'], '2026-11-15')
        self.assertEqual(g['deadlines'], ['2026-11-15', '2027-02-01'])

    def test_rolling_calls_publish_without_dates_and_sort_last(self):
        rows = [rec('r', deadline_type='Rolling / apply anytime', deadline_date=''), rec('f')]
        out = fg.publishable_grants(rows, TODAY)
        self.assertEqual([g['id'] for g in out], ['f', 'r'])
        self.assertTrue(out[1]['rolling'])

    def test_fixed_call_without_dates_is_skipped(self):
        self.assertEqual(self.ids([rec(deadline_date='')]), [])

    def test_unsafe_or_missing_links_are_skipped(self):
        rows = [rec('1', application_url='javascript:alert(1)'), rec('2', application_url=''),
                rec('3', application_url='www.example.org')]
        self.assertEqual(self.ids(rows), [])

    def test_checkbox_labels_collected_in_code_order(self):
        g = fg.publishable_grants([rec(career_stage___2='Mid-career', career_stage___10='Z')], TODAY)[0]
        self.assertEqual(g['career_stages'], ['Early-career', 'Mid-career', 'Z'])

    def test_empty_export_is_valid(self):
        self.assertEqual(fg.publishable_grants([], TODAY), [])


class Writing(unittest.TestCase):
    def test_write_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'data/grants.json'
            data = fg.publishable_grants([rec()], TODAY)
            self.assertTrue(fg.write_json_atomic(p, data))
            self.assertFalse(fg.write_json_atomic(p, data))

    def test_failed_fetch_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'grants.json'
            p.write_text('["KEEP"]')
            env = {'REDCAP_GRANTS_TOKEN': 'x', 'GRANTS_OUTPUT': str(p)}
            with patch.dict('os.environ', env), \
                 patch.object(fg, 'fetch_records', side_effect=RuntimeError('down')):
                self.assertEqual(fg.main(), 2)
            self.assertEqual(p.read_text(), '["KEEP"]')

    def test_missing_token_keeps_data_and_does_not_fail(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'grants.json'
            p.write_text('["KEEP"]')
            with patch.dict('os.environ', {'REDCAP_GRANTS_TOKEN': '', 'GRANTS_OUTPUT': str(p)}):
                self.assertEqual(fg.main(), 0)
            self.assertEqual(p.read_text(), '["KEEP"]')

    def test_api_error_payload_raises(self):
        with patch.object(fg, '_post', return_value={'error': 'You do not have permissions'}):
            with self.assertRaises(RuntimeError):
                fg.fetch_records('https://x/api/', 't', 'grant_details')


class SpreadsheetImport(unittest.TestCase):
    def test_osp_date_formats(self):
        cases = {
            '15 December 2026 (NIH listing; precise cutoff requires confirmation)': ['2026-12-15'],
            '1. 26/11/2024            2. 03/04/2025': ['2024-11-26', '2025-04-03'],
            '1. October 16, 2024   2. February 16, 2025      3. June, 16, 2025': ['2024-10-16', '2025-02-16', '2025-06-16'],
            '1. August 06, 2025.   2. August 06, 2026': ['2025-08-06', '2026-08-06'],
            'Apply anytime (rolling; check current scheme page)': [],
        }
        for text, want in cases.items():
            self.assertEqual([d.isoformat() for d in ic.parse_dates(text)], want, text)

    def test_budget_parsing(self):
        self.assertEqual(ic.parse_budget('£2 -£5 million.'), ('5000000', 'GBP'))
        self.assertEqual(ic.parse_budget(' $275,000. No more than $200,000 '), ('275000', 'USD'))
        self.assertEqual(ic.parse_budget("PI's Salary and up to £400,000 for research expenses"), ('400000', 'GBP'))
        self.assertEqual(ic.parse_budget('Not confirmed'), ('', ''))

    def test_imports_are_always_pending(self):
        out = ic.to_redcap({'application_url': 'https://x.org', 'grant_title': 'T', 'funder_name': 'NIH',
                            '_status_raw': 'Verified', '_deadline_raw': '1. 26/02/2025'}, 'sheet.xlsx', 7)
        self.assertEqual(out['osp_status'], '1')
        self.assertEqual(out['funder_name'], 'National Institutes of Health')
        self.assertEqual(out['deadline_date'], '2025-02-26')


if __name__ == '__main__':
    unittest.main()
