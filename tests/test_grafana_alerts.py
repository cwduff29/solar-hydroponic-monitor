#!/usr/bin/env python3
"""grafana/solarpi-grafana-alerts.py: rule table and payloads (no network)."""

import importlib.util
import os
import unittest
from unittest import mock

PATH = os.path.join(os.path.dirname(__file__), '..', 'grafana', 'solarpi-grafana-alerts.py')
spec = importlib.util.spec_from_file_location('solarpi_grafana_alerts', PATH)
ga = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ga)


class TestRules(unittest.TestCase):
    def test_rules_valid(self):
        ga.validate_rules(ga.RULES)

    def test_existing_down_rule_kept(self):
        down = [r for r in ga.RULES if r[0] == 'solarpi-down']
        self.assertEqual(len(down), 1)
        uid, title, expr, op, thr, pending, sev, nodata, execerr, _ = down[0]
        self.assertEqual((title, expr, op, thr, pending, sev, nodata, execerr),
                         ('Solar Pi Down', 'up{job="solar_hydroponic"}', 'lt', 1, '5m',
                          'warning', 'Alerting', 'Alerting'))
        # nothing else duplicates it
        self.assertEqual(sum('up{' in r[2] for r in ga.RULES), 1)

    def test_every_query_is_scoped_to_the_pi(self):
        for r in ga.RULES:
            self.assertIn('job="solar_hydroponic"', r[2], r[0])

    def test_payload_shape(self):
        p = ga.build_rule(ga.RULES[0], 'folder-1', 'ds-108')
        self.assertEqual(p['condition'], 'C')
        self.assertEqual(p['data'][0]['datasourceUid'], 'ds-108')
        self.assertEqual(p['data'][2]['model']['conditions'][0]['evaluator'],
                         {'type': 'lt', 'params': [1]})
        self.assertEqual(p['notification_settings'], {'receiver': 'HA Push'})
        self.assertEqual((p['folderUID'], p['ruleGroup']), ('folder-1', 'solarpi'))

    def test_duplicate_uid_rejected(self):
        with self.assertRaises(ValueError):
            ga.validate_rules(ga.RULES + [ga.RULES[0]])

    def test_refuses_pi_local_datasource(self):
        g = ga.Grafana('http://grafana.invalid', 't')
        ds = {'name': 'prometheus-solarpi', 'uid': 'abc', 'url': 'http://192.168.99.120:9090'}
        with mock.patch.object(g, 'request', return_value=(200, ds)):
            with self.assertRaises(SystemExit):
                g.datasource_uid('prometheus')

    def test_sends_disable_provenance_header(self):
        g = ga.Grafana('http://grafana.invalid', 'tok')
        seen = {}

        class Resp:
            status = 200
            def read(self): return b'{}'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout):
            seen.update(req.headers)
            return Resp()
        with mock.patch.object(ga.urllib.request, 'urlopen', fake_urlopen):
            g.request('GET', '/api/health')
        self.assertEqual(seen.get('X-disable-provenance'), 'true')
        self.assertEqual(seen.get('Authorization'), 'Bearer tok')


if __name__ == '__main__':
    unittest.main()
