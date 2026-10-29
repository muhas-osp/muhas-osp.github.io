import importlib.util
import json
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('queue_digest', ROOT / 'scripts/queue_digest.py')
qd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qd)
TODAY = date(2026, 11, 2)


def g(i, deadline=None, **kw):
    base = {'id': i, 'funder_name': 'Wellcome', 'grant_title': f'Call {i}', 'rolling': deadline is None,
            'next_deadline': deadline, 'application_url': f'https://x.org/{i}', 'tz_eligibility': 'Eligible',
            'award_budget': '', 'snapshot_summary': ''}
    base.update(kw)
    return base


class Content(unittest.TestCase):
    def test_new_and_closing_sections(self):
        grants = [g('1', '2026-11-20'), g('2', '2027-03-01'), g('3', '2026-11-10')]
        subject, body = qd.build_digest('weekly', grants, {'1', '2'}, TODAY)
        self.assertIn('1 new, 1 closing soon', subject)
        new, closing = body.split('CLOSING IN THE NEXT')
        self.assertIn('Call 3', new)
        self.assertIn('Call 1', closing)
        self.assertNotIn('Call 2', closing)          # open but not closing within 30 days

    def test_quiet_week_sends_nothing(self):
        self.assertIsNone(qd.build_digest('weekly', [g('1', '2027-03-01')], {'1'}, TODAY))

    def test_monthly_lists_everything(self):
        _, body = qd.build_digest('monthly', [g('1', '2027-03-01'), g('2')], {'1', '2'}, TODAY)
        self.assertIn('Call 1', body)
        self.assertIn('Call 2', body)

    def test_text_is_firewall_safe(self):
        grants = [g('1', '2026-11-20', grant_title='A; B <script>', snapshot_summary='x; y',
                    award_budget='up to 5m; renewable')]
        subject, body = qd.build_digest('weekly', grants, set(), TODAY)
        for text in (subject, body):
            self.assertNotIn(';', text)
            self.assertNotIn('<', text)


class Recipients(unittest.TestCase):
    STAFF = [
        {'record_id': '1', 'category': '1', 'employment_status': '1', 'pref_funding': '1', 'alert_frequency': '1', 'unsubscribed_all': ''},
        {'record_id': '2', 'category': '1', 'employment_status': '4', 'pref_funding': '1', 'alert_frequency': '1', 'unsubscribed_all': ''},
        {'record_id': '3', 'category': '5', 'employment_status': '1', 'pref_funding': '1', 'alert_frequency': '1', 'unsubscribed_all': ''},
        {'record_id': '4', 'category': '2', 'employment_status': '1', 'pref_funding': '0', 'alert_frequency': '1', 'unsubscribed_all': ''},
        {'record_id': '5', 'category': '1', 'employment_status': '2', 'pref_funding': '', 'alert_frequency': '', 'unsubscribed_all': '1'},
        {'record_id': '6', 'category': '3', 'employment_status': '6', 'pref_funding': '', 'alert_frequency': '3', 'unsubscribed_all': ''},
        {'record_id': '7', 'category': '9', 'employment_status': '4', 'pref_funding': '1', 'alert_frequency': '1', 'unsubscribed_all': ''},
        {'record_id': '8', 'category': '2', 'employment_status': '8', 'pref_funding': '', 'alert_frequency': '', 'unsubscribed_all': ''},
    ]

    def test_weekly(self):
        self.assertEqual(qd.recipients(self.STAFF, 'weekly'), ['1', '8'])

    def test_monthly(self):
        self.assertEqual(qd.recipients(self.STAFF, 'monthly'), ['6'])

    def test_no_personal_fields_requested(self):
        for f in ('email', 'first_names', 'surname', 'staff_no', 'birth'):
            self.assertFalse([x for x in qd.STAFF_FIELDS if f in x])


if __name__ == '__main__':
    unittest.main()
