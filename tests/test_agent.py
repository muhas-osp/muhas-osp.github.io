import importlib.util
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec_ = importlib.util.spec_from_file_location('agent', ROOT / 'agent/agent.py')
ag = importlib.util.module_from_spec(spec_)
import sys; sys.modules['agent'] = ag
spec_.loader.exec_module(ag)
SPEC = ag.load_dictionary()
TODAY = date(2026, 11, 3)

GOOD = {'funder_name': 'Wellcome', 'grant_title': 'Early-Career Awards', 'application_url': 'https://wellcome.org/eca',
        'snapshot_summary': 'Funds early-career researchers; up to GBP 400,000.', 'tz_eligibility': '1',
        'eligible_regions': ['6'], 'career_stage': ['1'], 'max_funding_amount': 400000, 'funding_currency': 'GBP',
        'deadline_type': '1', 'deadlines': ['2027-02-10'], 'deadline_time': '13:00', 'deadline_timezone': 'Europe/London',
        'evidence': {'deadline': 'Deadline 10 February 2027, 1pm UK time'}, 'uncertain_fields': []}


def usage(s=0, f=0):
    return NS(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0, cache_read_input_tokens=800,
              server_tool_use=NS(web_search_requests=s, web_fetch_requests=f))


def tool(name, inp, i='t1'):
    return NS(type='tool_use', name=name, input=inp, id=i)


class FakeClient:
    """Scripted Claude: returns the next response for each messages.create call; records requests."""
    def __init__(self, script):
        self.script, self.requests = list(script), []
        self.messages = self

    def create(self, **kw):
        self.requests.append(json.dumps(kw, default=str))
        return self.script.pop(0)


class Validation(unittest.TestCase):
    def test_good_record_valid(self):
        self.assertEqual(ag.validate_row(ag.to_row(GOOD, SPEC), SPEC), [])

    def test_bad_values_rejected(self):
        bad = dict(GOOD, deadlines=['10/02/2027'], deadline_timezone='UK time', deadline_time='1pm',
                   application_url='http://x.org')
        errs = ' '.join(ag.validate_row(ag.to_row(bad, SPEC), SPEC))
        for frag in ('YYYY-MM-DD', 'IANA', 'HH:MM', 'https'):
            self.assertIn(frag, errs)

    def test_agent_cannot_write_approval_fields(self):
        row = dict(ag.to_row(GOOD, SPEC), osp_status='2')
        self.assertIn('may not write', ' '.join(ag.validate_row(row, SPEC)))

    def test_text_is_firewall_safe(self):
        row = ag.to_row(dict(GOOD, snapshot_summary='A; B <b>x</b>'), SPEC)
        self.assertNotIn(';', row['snapshot_summary'])
        self.assertNotIn('<', row['snapshot_summary'])


class PreChecks(unittest.TestCase):
    EXIST = [{'record_id': '4', 'funder_name': 'Wellcome', 'grant_title': 'Wellcome Discovery Awards',
              'application_url': 'https://wellcome.org/research-funding/schemes/wellcome-discovery-awards/',
              'deadline_type': '1', 'deadline_date': '2026-12-01'}]

    def test_duplicate_by_url_and_title(self):
        self.assertIsNotNone(ag.find_duplicate({'application_url': 'https://www.wellcome.org/research-funding/schemes/wellcome-discovery-awards'}, self.EXIST))
        self.assertIsNotNone(ag.find_duplicate({'grant_title': 'Wellcome Discovery Awards', 'funder_name': 'Wellcome Trust'}, self.EXIST))
        self.assertIsNone(ag.find_duplicate({'grant_title': 'Career Development Awards', 'funder_name': 'Wellcome'}, self.EXIST))

    def test_search_page_url_is_not_a_duplicate_key(self):
        e = [{'record_id': '1', 'application_url': 'https://www.grants.nih.gov/funding/explore-nih-opportunities', 'grant_title': 'X'}]
        self.assertIsNone(ag.find_duplicate({'application_url': 'https://grants.nih.gov/funding/explore-nih-opportunities', 'grant_title': 'Y'}, e))

    def test_deadline_change(self):
        new = {'deadline_type': '1', 'deadline_date': '2026-12-15'}
        self.assertIn('2026-12-01', ag.deadline_change(new, self.EXIST[0], TODAY))
        self.assertEqual(ag.deadline_change({'deadline_type': '1', 'deadline_date': '2026-12-01'}, self.EXIST[0], TODAY), '')

    def test_review_rules(self):
        ok = {'status': 'ok'}
        self.assertEqual(ag.review_reasons(ag.to_row(GOOD, SPEC), [], ok), [])
        r = ag.review_reasons(ag.to_row(dict(GOOD, deadline_timezone='', tz_eligibility='3'), SPEC), ['budget'], ok)
        self.assertEqual(len(r), 3)


def _have(mod):
    return importlib.util.find_spec(mod) is not None


@unittest.skipUnless(_have('anthropic') and _have('openpyxl'), 'needs anthropic + openpyxl (discover workflow installs them)')
class FullRun(unittest.TestCase):
    def test_dry_run_end_to_end(self):
        existing = [{'record_id': '4', 'funder_name': 'Wellcome', 'grant_title': 'Wellcome Discovery Awards',
                     'application_url': 'https://wellcome.org/discovery', 'deadline_type': '2'},
                    {'record_id': '7', 'funder_name': 'NIH', 'grant_title': 'Old call',
                     'application_url': 'https://grants.nih.gov/gone', 'deadline_type': '1', 'deadline_date': '2027-01-01'}]
        script = [
            # discovery (Wellcome): one search, then report 3 candidates
            NS(stop_reason='tool_use', usage=usage(s=1), content=[tool('list_existing', {'query': 'Wellcome'})]),
            NS(stop_reason='tool_use', usage=usage(s=1, f=1), content=[tool('report_candidates', {'candidates': [
                {'url': 'https://wellcome.org/discovery', 'title': 'Discovery Awards'},
                {'url': 'https://wellcome.org/eca', 'title': 'Early-Career Awards'},
                {'url': 'https://wellcome.org/news', 'title': 'News item'}]})]),
            # extraction 1: first submission invalid, then corrected
            NS(stop_reason='tool_use', usage=usage(f=1), content=[tool('create_pending', dict(GOOD, deadlines=['10 Feb 2027']))]),
            NS(stop_reason='tool_use', usage=usage(), content=[tool('create_pending', GOOD)]),
            # extraction 2: not a call
            NS(stop_reason='tool_use', usage=usage(f=1), content=[tool('not_a_call', {'reason': 'news article'})]),
        ]
        fake = FakeClient(script)
        links = {'https://grants.nih.gov/gone': {'status': 'dead', 'code': 404}}
        with tempfile.TemporaryDirectory() as d, \
             patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-SECRET-KEY', 'REDCAP_AGENT_TOKEN': 'REDCAP-SECRET',
                                     'AGENT_DRY_RUN': '1', 'AGENT_FUNDERS': 'Wellcome', 'AGENT_OUT': d,
                                     'GITHUB_ACTIONS': ''}), \
             patch.object(ag, 'redcap_existing', return_value=existing), \
             patch.object(ag, 'redcap_import') as imp, \
             patch.object(ag, 'check_link', side_effect=lambda u: links.get(u, {'status': 'ok', 'code': 200})), \
             patch.object(ag, 'ROOT', Path(d)), \
             patch('anthropic.Anthropic', return_value=fake):
            (Path(d) / 'data').mkdir()
            self.assertEqual(ag.main(), 0)
            imp.assert_not_called()                                   # dry run writes nothing to REDCap
            from openpyxl import load_workbook
            wb = load_workbook(next(Path(d).glob('agent_report_*.xlsx')))
            drafts = list(wb['New drafts'].values)
            self.assertEqual(len(drafts), 2)                           # header + 1 draft
            row = dict(zip(drafts[0], drafts[1]))
            self.assertEqual(row['needs_review'], '0')
            self.assertEqual(row['deadline_timezone'], 'Europe/London')
            outcomes = [r[3] for r in list(wb['Candidates'].values)[1:]]
            self.assertIn('already in REDCap (record 4)', outcomes)
            self.assertIn('not a call', outcomes)
            flagged = [r for r in list(wb['Existing records'].values)[1:] if r[3]]
            self.assertTrue(any('dead link' in str(r[3]) for r in flagged))
            usage_log = (Path(d) / 'data/agent_usage.csv').read_text()
            self.assertIn('extraction', usage_log)
        for req in fake.requests:                                     # secrets never reach the model
            self.assertNotIn('SECRET', req)
        self.assertIn('cache_control', fake.requests[0])               # cached system prompt


if __name__ == '__main__':
    unittest.main()
