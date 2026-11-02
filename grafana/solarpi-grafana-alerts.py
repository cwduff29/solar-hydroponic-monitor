#!/usr/bin/env python3
"""
solarpi-grafana-alerts.py - provision the Solar Pi alert rules in Grafana.

Creates or updates each rule in RULES by its fixed UID through the Grafana
alerting provisioning API, in folder "Solar Hydroponic", group "solarpi",
notifying contact point "HA Push". Rules are sent with
X-Disable-Provenance so they stay editable in the Grafana UI. Rules in the
folder that are not listed here are left alone.

Every rule queries the "prometheus" datasource (central Prometheus, LXC 108),
never "prometheus-solarpi" (the Pi's local Prometheus, being retired).

Usage:
    GRAFANA_TOKEN=glsa_... ./solarpi-grafana-alerts.py [--url URL] [--dry-run]

GRAFANA_TOKEN is an Admin service-account token. --dry-run prints the rule
payloads and makes no requests.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

GRAFANA_URL = os.environ.get('GRAFANA_URL', 'http://192.168.99.224:3000')
DATASOURCE = 'prometheus'              # UID or name; must be the LXC 108 Prometheus
FORBIDDEN_DATASOURCES = ('prometheus-solarpi',)
FOLDER_TITLE = 'Solar Hydroponic'
RULE_GROUP = 'solarpi'
CONTACT_POINT = 'HA Push'

J = 'job="solar_hydroponic"'

# (uid, title, PromQL, op, threshold, for, severity, noData, execErr, summary)
#
# Each query returns a value; the rule fires when `value <op> threshold` for
# the pending period. op is one of gt, lt. Queries filtered with `and` return
# nothing when the filter fails, so those rules use noData=OK. Pi down is
# covered once by solarpi-down; the others use noData=OK so they don't all
# fire alongside it.
RULES = [
    ('solarpi-down', 'Solar Pi Down',
     f'up{{{J}}}', 'lt', 1, '5m', 'warning', 'Alerting', 'Alerting',
     'Prometheus (LXC 108) cannot scrape the solar Pi node_exporter.'),

    ('solarpi-rebooted', 'Solar Pi Rebooted',
     f'time() - node_boot_time_seconds{{{J}}}', 'lt', 900, '0s', 'warning', 'OK', 'Error',
     'The solar Pi booted {{ humanizeDuration $values.B.Value }} ago '
     '(power loss or buck converter brownout?).'),

    ('solarpi-undervoltage', 'Solar Pi 5V Under-voltage',
     f'max_over_time(pi_undervoltage_alarm{{{J}}}[10m])', 'gt', 0, '0s', 'warning', 'OK', 'Error',
     'The Pi reported 5V supply under-voltage in the last 10 minutes.'),

    ('solarpi-data-stale', 'Solar Pi Data Stale',
     f'time() - node_textfile_mtime_seconds{{{J},'
     f'file=~"/ramdisk/(Renogy|waterflow|battery_shutdown)\\\\.prom"}}',
     'gt', 300, '5m', 'warning', 'OK', 'Error',
     '{{ $labels.file }} has not been updated for {{ humanizeDuration $values.B.Value }}; '
     'is its service running?'),

    ('solarpi-battery-low-voltage', 'Solar Battery Low Voltage',
     f'battery_voltage_rest_estimate{{{J}}}', 'lt', 12.6, '10m', 'warning', 'OK', 'Error',
     'Battery rest voltage {{ printf "%.2f" $values.B.Value }} V (below 12.6 V, about 14% SOC).'),

    ('solarpi-battery-critical-voltage', 'Solar Battery Critical Voltage',
     f'battery_voltage_rest_estimate{{{J}}}', 'lt', 12.2, '5m', 'critical', 'OK', 'Error',
     'Battery rest voltage {{ printf "%.2f" $values.B.Value }} V (below 12.2 V, about 6% SOC); '
     'the controller LVD is close.'),

    ('solarpi-soc-low', 'Solar Battery SOC Low',
     f'battery_soc{{{J}}} and on(instance) (battery_soc_confidence{{{J}}} >= 0.3)',
     'lt', 20, '15m', 'warning', 'OK', 'Error',
     'Estimated SOC {{ printf "%.0f" $values.B.Value }}% (below 20%).'),

    ('solarpi-soc-register-disagrees', 'Solar SOC Register Disagrees',
     f'abs(battery_soc_hw{{{J}}} - battery_soc{{{J}}}) '
     f'and on(instance) (battery_soc_confidence{{{J}}} >= 0.5)',
     'gt', 30, '1h', 'info', 'OK', 'Error',
     'The controller SOC register differs from the estimate by '
     '{{ printf "%.0f" $values.B.Value }} points; do not trust the register.'),

    ('solarpi-over-discharge', 'Solar Battery Over-discharge',
     f'max_over_time(total_battery_over_discharges{{{J}}}[1h]) '
     f'- max_over_time(total_battery_over_discharges{{{J}}}[7d] offset 1h)',
     'gt', 0, '0s', 'critical', 'OK', 'Error',
     'The charge controller counted a new battery over-discharge (LVD trip).'),

    ('solarpi-energy-deficit', 'Solar Daily Energy Deficit',
     f'(max_over_time(total_charging_ah{{{J}}}[24h]) - min_over_time(total_charging_ah{{{J}}}[24h])) '
     f'- (max_over_time(total_discharging_ah{{{J}}}[24h]) - min_over_time(total_discharging_ah{{{J}}}[24h]))',
     'lt', -10, '1h', 'warning', 'OK', 'Error',
     'Net {{ printf "%.0f" $values.B.Value }} Ah into the battery over the last 24h '
     '(more than 10 Ah drawn than charged).'),

    ('solarpi-aerator-shed', 'Solar Aerator Shed (Low Battery)',
     f'waterflow_load_shed_tier{{{J}}}', 'gt', 1, '10m', 'warning', 'OK', 'Error',
     'Load shedding has disabled the aerator because of low battery SOC.'),

    ('solarpi-shutdown-condition', 'Solar Battery Shutdown Condition',
     f'battery_shutdown_condition{{{J}}}', 'gt', 0, '0s', 'critical', 'OK', 'Error',
     'battery_shutdown reports the critical condition (SOC, confidence and rest voltage all low). '
     'Check battery_shutdown_mode: in halt mode the Pi is shutting down.'),
]


def build_rule(rule, folder_uid, datasource_uid):
    """Grafana provisioning payload for one RULES entry."""
    (uid, title, expr, op, threshold, pending, severity,
     no_data, exec_err, summary) = rule
    return {
        'uid': uid,
        'title': title,
        'folderUID': folder_uid,
        'ruleGroup': RULE_GROUP,
        'condition': 'C',
        'data': [
            {
                'refId': 'A',
                'relativeTimeRange': {'from': 600, 'to': 0},
                'datasourceUid': datasource_uid,
                'model': {'refId': 'A', 'expr': expr, 'instant': True, 'range': False,
                          'intervalMs': 1000, 'maxDataPoints': 43200},
            },
            {
                'refId': 'B',
                'relativeTimeRange': {'from': 0, 'to': 0},
                'datasourceUid': '__expr__',
                'model': {'refId': 'B', 'type': 'reduce', 'expression': 'A',
                          'reducer': 'last', 'settings': {'mode': 'dropNN'}},
            },
            {
                'refId': 'C',
                'relativeTimeRange': {'from': 0, 'to': 0},
                'datasourceUid': '__expr__',
                'model': {'refId': 'C', 'type': 'threshold', 'expression': 'B',
                          'conditions': [{'evaluator': {'type': op, 'params': [threshold]}}]},
            },
        ],
        'noDataState': no_data,
        'execErrState': exec_err,
        'for': pending,
        'labels': {'severity': severity, 'system': 'solarpi'},
        'annotations': {'summary': summary},
        'isPaused': False,
        'notification_settings': {'receiver': CONTACT_POINT},
    }


def validate_rules(rules):
    """Raise ValueError on duplicate UIDs or malformed entries."""
    uids = [r[0] for r in rules]
    if len(uids) != len(set(uids)):
        raise ValueError(f'duplicate rule UIDs: {sorted(u for u in uids if uids.count(u) > 1)}')
    for r in rules:
        if len(r) != 10:
            raise ValueError(f'{r[0]}: expected 10 fields, got {len(r)}')
        if r[3] not in ('gt', 'lt'):
            raise ValueError(f'{r[0]}: op must be gt or lt')
        if r[7] not in ('OK', 'NoData', 'Alerting') or r[8] not in ('OK', 'Error', 'Alerting'):
            raise ValueError(f'{r[0]}: bad noData/execErr state')
        if len(r[0]) > 40:
            raise ValueError(f'{r[0]}: UID longer than 40 characters')


class Grafana:
    def __init__(self, url, token):
        self.url = url.rstrip('/')
        self.token = token

    def request(self, method, path, body=None):
        """Returns (status, parsed JSON or None)."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'X-Disable-Provenance': 'true',
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw) if raw else None
            except ValueError:
                return e.code, {'message': raw.decode(errors='replace')}

    def datasource_uid(self, ref):
        for path in (f'/api/datasources/uid/{urllib.parse.quote(ref)}',
                     f'/api/datasources/name/{urllib.parse.quote(ref)}'):
            status, ds = self.request('GET', path)
            if status == 200:
                if ds.get('name') in FORBIDDEN_DATASOURCES or ds.get('uid') in FORBIDDEN_DATASOURCES:
                    sys.exit(f'Refusing to use datasource {ds.get("name")!r} ({ds.get("url")}); '
                             f'rules must query LXC 108')
                print(f'Datasource: {ds["name"]} uid={ds["uid"]} url={ds.get("url")}')
                return ds['uid']
        sys.exit(f'Datasource {ref!r} not found (tried UID and name)')

    def folder_uid(self, title):
        status, folders = self.request('GET', '/api/folders?limit=1000')
        if status != 200:
            sys.exit(f'Listing folders failed: HTTP {status} {folders}')
        for f in folders:
            if f['title'] == title:
                return f['uid']
        status, f = self.request('POST', '/api/folders', {'title': title})
        if status not in (200, 201):
            sys.exit(f'Creating folder {title!r} failed: HTTP {status} {f}')
        print(f'Created folder {title!r}')
        return f['uid']

    def upsert_rule(self, payload):
        uid = payload['uid']
        status, _ = self.request('GET', f'/api/v1/provisioning/alert-rules/{uid}')
        if status == 200:
            status, resp = self.request('PUT', f'/api/v1/provisioning/alert-rules/{uid}', payload)
            action = 'updated'
        elif status == 404:
            status, resp = self.request('POST', '/api/v1/provisioning/alert-rules', payload)
            action = 'created'
        else:
            return False, f'lookup failed: HTTP {status}'
        if status in (200, 201):
            return True, action
        return False, f'{action[:-1]} failed: HTTP {status} {resp}'


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--url', default=GRAFANA_URL, help=f'Grafana URL (default {GRAFANA_URL})')
    ap.add_argument('--dry-run', action='store_true', help='print payloads, make no requests')
    args = ap.parse_args()

    validate_rules(RULES)

    if args.dry_run:
        payloads = [build_rule(r, '<folder-uid>', f'<uid of {DATASOURCE}>') for r in RULES]
        print(json.dumps(payloads, indent=2))
        return 0

    token = os.environ.get('GRAFANA_TOKEN')
    if not token:
        sys.exit('Set GRAFANA_TOKEN to an Admin service-account token')
    g = Grafana(args.url, token)
    ds_uid = g.datasource_uid(DATASOURCE)
    folder = g.folder_uid(FOLDER_TITLE)

    failed = 0
    for rule in RULES:
        ok, msg = g.upsert_rule(build_rule(rule, folder, ds_uid))
        print(f'{"OK  " if ok else "FAIL"} {rule[0]:34s} {rule[1]}: {msg}')
        failed += not ok
    print(f'{len(RULES) - failed}/{len(RULES)} rules in {FOLDER_TITLE}/{RULE_GROUP} '
          f'-> contact point {CONTACT_POINT!r}')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
