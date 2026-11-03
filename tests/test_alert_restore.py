#!/usr/bin/env python3
"""A flow alert that was active when waterflow stopped must clear once flow is
healthy after a restart (it used to stay 'active' on disk forever)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))


class TestRestoredFlowAlerts(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from fake_hw import import_waterflow
        cls.w = import_waterflow()

    def setUp(self):
        w = self.w
        self.emails = []
        patches = [
            mock.patch.object(w, 'send_email_alert', lambda subj, body: self.emails.append(subj)),
            mock.patch.object(w, 'aeration_state', False),
            mock.patch.object(w, 'last_aeration_toggle', 0),
            mock.patch.object(w, 'DISABLE_FLOW_ALERTS', False),
            mock.patch.object(w, 'MAIN_PUMP_TARGET_FLOW', 0),
            mock.patch.object(w, 'attempt_pump_recovery', mock.Mock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        # Fresh process: module flags at their defaults
        for name, value in (('leak_alert_sent', False), ('low_flow_alert_sent', False),
                            ('flow_imbalance_start_time', None), ('low_flow_start_time', None),
                            ('recovery_attempted', False), ('backup_pump_active', False),
                            ('main_pump_planned_off', False), ('main_pump_planned_restart', None),
                            ('smoothed_flow_inlet', 0.0), ('smoothed_flow_outlet', 0.0),
                            ('flow_history_inlet', []), ('flow_history_outlet', [])):
            setattr(w, name, value)
        for a in w._FLOW_ALERT_TYPES:
            w.alert_manager.clear(a)

    def _restore_active(self, alert_type):
        """Simulate an alert that was active in the state file at startup."""
        am = self.w.alert_manager
        am.should_send(alert_type)
        am.mark_sent(alert_type)
        am._state[alert_type]['last_cleared'] = None
        self.assertTrue(am.is_active(alert_type))
        self.w.sync_alert_flags()

    def _healthy_flow(self, ticks=6):
        w = self.w
        for _ in range(ticks):
            w.count_inlet, w.count_outlet = 127, 120          # ~1.7 L/min, balanced
            w.monitor_flow()

    def test_restored_leak_alert_clears_on_balanced_flow(self):
        self._restore_active('leak_detected')
        self.assertTrue(self.w.leak_alert_sent)             # metric reports it until cleared
        self._healthy_flow()
        self.assertFalse(self.w.alert_manager.is_active('leak_detected'))
        self.assertFalse(self.w.leak_alert_sent)
        self.assertEqual(self.emails, ['Flow Balance Restored'])

    def test_restored_low_flow_alert_clears_on_healthy_flow(self):
        self._restore_active('low_flow')
        self._healthy_flow()
        self.assertFalse(self.w.alert_manager.is_active('low_flow'))
        self.assertEqual(self.emails, ['Water Flow Restored'])

    def test_no_restored_alert_sends_nothing(self):
        self.w.sync_alert_flags()
        self._healthy_flow()
        self.assertEqual(self.emails, [])

    def test_restored_leak_alert_no_longer_blocks_low_soc_cycling(self):
        w = self.w
        self._restore_active('leak_detected')
        self.assertTrue(any(w.alert_manager.is_active(a) for a in w._FLOW_ALERT_TYPES))
        self._healthy_flow()
        self.assertFalse(any(w.alert_manager.is_active(a) for a in w._FLOW_ALERT_TYPES))


if __name__ == '__main__':
    unittest.main()
