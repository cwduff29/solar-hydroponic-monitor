"""Import waterflow_enhanced_failsafe without hardware: fake RPi.GPIO, no
email, and all state/log/prom paths redirected into a temp directory."""

import os
import sys
import tempfile
import types

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


class FakePWM:
    def __init__(self, pin, freq):
        self.duty = None

    def start(self, duty):
        self.duty = duty

    def ChangeDutyCycle(self, duty):
        self.duty = duty

    def stop(self):
        self.duty = 0


def import_waterflow():
    if 'waterflow_enhanced_failsafe' in sys.modules:
        return sys.modules['waterflow_enhanced_failsafe']
    sys.path.insert(0, REPO)
    gpio = types.ModuleType('RPi.GPIO')
    for name in ('BCM', 'OUT', 'IN', 'BOTH', 'FALLING', 'RISING', 'PUD_UP', 'PUD_DOWN'):
        setattr(gpio, name, name)
    gpio.HIGH, gpio.LOW = 1, 0
    gpio.outputs = {}
    gpio.output = lambda pin, value: gpio.outputs.__setitem__(pin, value)
    gpio.input = lambda pin: 1
    gpio.PWM = FakePWM
    for name in ('setmode', 'setwarnings', 'setup', 'cleanup',
                 'add_event_detect', 'remove_event_detect'):
        setattr(gpio, name, lambda *a, **k: None)
    rpi = types.ModuleType('RPi')
    rpi.GPIO = gpio
    sys.modules['RPi'] = rpi
    sys.modules['RPi.GPIO'] = gpio

    import monitor_common
    tmp = tempfile.mkdtemp(prefix='waterflow_test_')
    overrides = {
        'paths.waterflow_log': f'{tmp}/waterflow.log',
        'paths.waterflow_prom': f'{tmp}/waterflow.prom',
        'paths.waterflow_prom_tmp': f'{tmp}/waterflow.prom.tmp',
        'paths.waterflow_alert_state': f'{tmp}/alerts.json',
        'paths.waterflow_persistent_state': f'{tmp}/state/waterflow_state.json',
        'paths.battery_data_file': f'{tmp}/Renogy.prom',
        'watchdog.enabled': False,
    }
    real_get = monitor_common.get_config
    monitor_common.get_config = lambda key, default=None: overrides.get(key, real_get(key, default))
    monitor_common.send_email = lambda *a, **k: True
    monitor_common.startup_selftest = lambda *a, **k: None

    import logging
    logging.disable(logging.CRITICAL)
    import waterflow_enhanced_failsafe as w
    w.send_email_alert = lambda *a, **k: True
    w.TEST_TMP = tmp
    return w
