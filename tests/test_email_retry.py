#!/usr/bin/env python3
"""send_email: SMTP timeout, and background retry of transient failures
(DNS not up yet after boot). No network: smtplib.SMTP is mocked."""

import os
import smtplib
import socket
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import monitor_common as mc  # noqa: E402

CFG = {'alerts.email_retry_interval_seconds': 0.01, 'alerts.email_retry_max_hours': 6,
       'alerts.email_retry_max_queued': 20, 'alerts.smtp_timeout_seconds': 20}


class FakeSMTP:
    """Context-manager SMTP double; behaviour comes from a shared script."""
    script = []      # list of exceptions / None, consumed one per connection
    sent = []
    calls = []

    def __init__(self, host, port, timeout=None):
        FakeSMTP.calls.append({'host': host, 'port': port, 'timeout': timeout})
        outcome = FakeSMTP.script.pop(0) if FakeSMTP.script else None
        if outcome is not None:
            raise outcome

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        FakeSMTP.sent.append((msg['Subject'], msg.get_content()))


class TestEmailRetry(unittest.TestCase):
    def setUp(self):
        FakeSMTP.script, FakeSMTP.sent, FakeSMTP.calls = [], [], []
        patches = [
            mock.patch.object(mc.smtplib, 'SMTP', FakeSMTP),
            mock.patch.object(mc, 'get_config', lambda k, d=None: CFG.get(k, d)),
            mock.patch.dict(os.environ, {'SMTP_USER': 'u@example.com', 'SMTP_PASSWORD': 'pw',
                                         'SMTP_RECIPIENTS': 'r@example.com'}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        mc._email_queue.clear()

    def _wait_queue_empty(self, timeout=2.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with mc._email_queue_lock:
                if not mc._email_queue and mc._email_retry_thread is None:
                    return
            time.sleep(0.005)
        self.fail('retry queue did not drain')

    def test_smtp_has_timeout(self):
        self.assertTrue(mc.send_email('s', 'b'))
        self.assertEqual(FakeSMTP.calls[0]['timeout'], 20)
        self.assertEqual(FakeSMTP.sent, [('s', 'b\n')])

    def test_dns_failure_is_queued_and_delivered_later(self):
        dns = socket.gaierror(-3, 'Temporary failure in name resolution')
        FakeSMTP.script = [dns, dns, None]
        with self.assertLogs(level='WARNING') as logs:
            self.assertTrue(mc.send_email('System Online: renogy', 'hello'))
            self._wait_queue_empty()
        self.assertEqual(len(FakeSMTP.sent), 1)
        subject, body = FakeSMTP.sent[0]
        self.assertEqual(subject, 'System Online: renogy')
        self.assertIn('Delayed delivery', body)
        self.assertTrue(any('sent after' in m for m in logs.output))

    def test_queued_emails_keep_order(self):
        FakeSMTP.script = [TimeoutError('timed out'), TimeoutError('timed out')]
        with self.assertLogs(level='WARNING'):
            mc.send_email('first', 'a')
            mc.send_email('second', 'b')
            self._wait_queue_empty()
        self.assertEqual([s for s, _ in FakeSMTP.sent], ['first', 'second'])

    def test_auth_failure_is_not_retried(self):
        FakeSMTP.script = [smtplib.SMTPAuthenticationError(535, b'bad credentials')]
        with self.assertLogs(level='ERROR'):
            self.assertFalse(mc.send_email('s', 'b'))
        self.assertEqual(mc._email_queue, [])

    def test_gives_up_after_max_age(self):
        CFG_OLD = dict(CFG)
        self.addCleanup(lambda: (CFG.clear(), CFG.update(CFG_OLD)))
        CFG['alerts.email_retry_max_hours'] = 0
        FakeSMTP.script = [socket.gaierror(-3, 'dns')]
        with self.assertLogs(level='WARNING') as logs:
            self.assertTrue(mc.send_email('s', 'b'))
            self._wait_queue_empty()
        self.assertEqual(FakeSMTP.sent, [])
        self.assertTrue(any('Giving up' in m for m in logs.output))

    def test_missing_credentials(self):
        with mock.patch.dict(os.environ, {'SMTP_USER': ''}):
            with self.assertLogs(level='ERROR'):
                self.assertFalse(mc.send_email('s', 'b'))


if __name__ == '__main__':
    unittest.main()
