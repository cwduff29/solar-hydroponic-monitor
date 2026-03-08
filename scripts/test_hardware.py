#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
test_hardware.py - Interactive hardware test script for solar hydroponic monitor

Usage: python3 scripts/test_hardware.py

Tests:
  1. Serial port (/dev/serial0)
  2. I2C / BME280 sensor
  3. 1-Wire / DS18B20 temperature sensors
  4. Relay outputs (main_pump, backup_pump, aeration, fan)
  5. Flow sensors (inlet / outlet pulse count)

Reads GPIO pin config from config.json if present, otherwise uses hardcoded
defaults matching the project's config.json values.

Requires: RPi.GPIO, smbus2, bme280 (gracefully skipped if not on Pi hardware)
"""

import os
import sys
import json
import time
import glob

# ============================================================================
# ANSI COLOR CODES
# ============================================================================

GREEN  = '\033[92m'
RED    = '\033[91m'
YELLOW = '\033[93m'
BOLD   = '\033[1m'
RESET  = '\033[0m'

def green(s):  return f"{GREEN}{s}{RESET}"
def red(s):    return f"{RED}{s}{RESET}"
def yellow(s): return f"{YELLOW}{s}{RESET}"
def bold(s):   return f"{BOLD}{s}{RESET}"


# ============================================================================
# CONFIG LOADING
# ============================================================================

_DEFAULT_CONFIG = {
    'waterflow': {
        'gpio': {
            'flow_sensor_inlet':  17,
            'flow_sensor_outlet': 27,
            'main_pump_relay':    22,
            'backup_pump_relay':  23,
            'aeration_pump':      24,
            'fan_control':        25,
        },
        'bme280': {
            'i2c_address': 0x76,
        },
        'relay': {
            'main_pump_terminal':   'NC',
            'backup_pump_terminal': 'NO',
            'aeration_terminal':    'NO',
            'fan_terminal':         'NC',
            'active_low_board':     True,
        },
    }
}


def load_config():
    """Load config.json from parent directory, fall back to defaults."""
    config_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                               '..', 'config.json')
    config_path = os.path.normpath(config_path)
    try:
        with open(config_path, 'r') as f:
            cfg = json.load(f)
        print(f"  Loaded config from {config_path}")
        return cfg
    except Exception as e:
        print(yellow(f"  Could not load config.json ({e}); using hardcoded defaults"))
        return _DEFAULT_CONFIG


def get_cfg(config, key, default=None):
    """Dot-separated key lookup."""
    parts = key.split('.')
    node = config
    for part in parts:
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return default
    return node


# ============================================================================
# HELPERS
# ============================================================================

def prompt_continue(label=""):
    """Wait for user to press Enter."""
    msg = f"\n  Press Enter to continue{': ' + label if label else ''}..."
    try:
        input(msg)
    except EOFError:
        pass


def section(title):
    print()
    print(bold("=" * 60))
    print(bold(f"  {title}"))
    print(bold("=" * 60))


# ============================================================================
# GPIO IMPORT
# ============================================================================

GPIO = None
GPIO_AVAILABLE = False

try:
    import RPi.GPIO as _GPIO
    GPIO = _GPIO
    GPIO_AVAILABLE = True
except ImportError:
    print(yellow("  RPi.GPIO not available — relay and flow sensor tests will be skipped."))
    print(yellow("  (Not running on Pi hardware, or RPi.GPIO not installed.)"))


# ============================================================================
# TEST RESULTS TRACKER
# ============================================================================

results = []  # list of (name, status, detail)


def record(name, status, detail=""):
    """Record a test result. status: 'PASS', 'WARN', 'FAIL'"""
    results.append((name, status, detail))
    if status == 'PASS':
        print(f"  {green('[PASS]')} {name}" + (f" — {detail}" if detail else ""))
    elif status == 'WARN':
        print(f"  {yellow('[WARN]')} {name}" + (f" — {detail}" if detail else ""))
    else:
        print(f"  {red('[FAIL]')} {name}" + (f" — {detail}" if detail else ""))


# ============================================================================
# TEST 1: SERIAL PORT
# ============================================================================

def test_serial_port():
    section("Test 1/6: Serial Port (/dev/serial0)")
    port = '/dev/serial0'
    if not os.path.exists(port):
        record("Serial port exists", "FAIL", f"{port} not found")
        return
    record("Serial port exists", "PASS", port)

    try:
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        os.close(fd)
        record("Serial port openable", "PASS", "opened and closed successfully")
    except OSError as e:
        record("Serial port openable", "FAIL", str(e))


# ============================================================================
# TEST 2: I2C / BME280
# ============================================================================

def test_bme280(config):
    section("Test 2/6: I2C / BME280")

    i2c_addr = get_cfg(config, 'waterflow.bme280.i2c_address', 0x76)
    # config.json stores address as integer (118 = 0x76)
    if isinstance(i2c_addr, int) and i2c_addr > 0x7F:
        # stored as decimal 118 → 0x76
        pass
    print(f"  BME280 I2C address: 0x{i2c_addr:02X}")

    try:
        import smbus2
        import bme280 as _bme280
    except ImportError as e:
        record("BME280 import", "FAIL", f"ImportError: {e}")
        return

    try:
        bus = smbus2.SMBus(1)
        calibration_params = _bme280.load_calibration_params(bus, i2c_addr)
        data = _bme280.sample(bus, i2c_addr, calibration_params)
        record(
            "BME280 read",
            "PASS",
            f"temp={data.temperature:.1f}°C  humidity={data.humidity:.1f}%  "
            f"pressure={data.pressure:.1f}hPa"
        )
        bus.close()
    except Exception as e:
        record("BME280 read", "FAIL", str(e))


# ============================================================================
# TEST 3: 1-WIRE / DS18B20
# ============================================================================

def test_ds18b20():
    section("Test 3/6: 1-Wire / DS18B20 Temperature Sensors")

    ds_pattern = '/sys/bus/w1/devices/28-*'
    folders = sorted(glob.glob(ds_pattern))

    if not folders:
        record("DS18B20 sensors found", "FAIL",
               "No sensors found — check 1-Wire is enabled and wiring")
        return

    record("DS18B20 sensors found", "PASS", f"{len(folders)} sensor(s)")

    for folder in folders:
        sensor_id = os.path.basename(folder)
        try:
            with open(os.path.join(folder, 'w1_slave'), 'r') as f:
                lines = f.readlines()
            if len(lines) >= 2 and 'YES' in lines[0]:
                eq_pos = lines[1].find('t=')
                if eq_pos != -1:
                    raw = int(lines[1][eq_pos + 2:].strip())
                    celsius = raw / 1000.0
                    fahrenheit = celsius * 9.0 / 5.0 + 32.0
                    record(
                        f"DS18B20 {sensor_id}",
                        "PASS",
                        f"{celsius:.2f}°C / {fahrenheit:.2f}°F"
                    )
                else:
                    record(f"DS18B20 {sensor_id}", "FAIL", "no t= in data")
            else:
                record(f"DS18B20 {sensor_id}", "FAIL", "CRC check failed")
        except Exception as e:
            record(f"DS18B20 {sensor_id}", "FAIL", str(e))


# ============================================================================
# TEST 4: RELAY TEST
# ============================================================================

def get_relay_state_gpio(device_on, terminal_type, active_low):
    """Calculate GPIO output level for a relay."""
    if terminal_type == "NC":
        relay_energized = not device_on
    else:  # NO
        relay_energized = device_on
    if active_low:
        return GPIO.LOW if relay_energized else GPIO.HIGH
    else:
        return GPIO.HIGH if relay_energized else GPIO.LOW


def test_relays(config):
    section("Test 4/6: Relay Test")

    if not GPIO_AVAILABLE:
        record("Relay test", "WARN", "RPi.GPIO not available — skipping")
        return

    active_low = get_cfg(config, 'waterflow.relay.active_low_board', True)
    relays = [
        ('main_pump',   get_cfg(config, 'waterflow.gpio.main_pump_relay',   22),
                        get_cfg(config, 'waterflow.relay.main_pump_terminal',   'NC')),
        ('backup_pump', get_cfg(config, 'waterflow.gpio.backup_pump_relay', 23),
                        get_cfg(config, 'waterflow.relay.backup_pump_terminal', 'NO')),
        ('aeration',    get_cfg(config, 'waterflow.gpio.aeration_pump',     24),
                        get_cfg(config, 'waterflow.relay.aeration_terminal',    'NO')),
        ('fan',         get_cfg(config, 'waterflow.gpio.fan_control',       25),
                        get_cfg(config, 'waterflow.relay.fan_terminal',         'NC')),
    ]

    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        for name, pin, terminal in relays:
            GPIO.setup(pin, GPIO.OUT)
            # Start with device OFF
            GPIO.output(pin, get_relay_state_gpio(False, terminal, active_low))
    except Exception as e:
        record("GPIO setup", "FAIL", str(e))
        return

    record("GPIO setup", "PASS", "all relay pins configured as outputs")

    for name, pin, terminal in relays:
        print()
        print(f"  Testing relay: {bold(name)} (GPIO {pin}, terminal={terminal})")
        prompt_continue(f"ready to activate {name}")

        print(f"  Turning ON {name} (GPIO {pin})...")
        try:
            GPIO.output(pin, get_relay_state_gpio(True, terminal, active_low))
        except Exception as e:
            record(f"Relay {name} ON", "FAIL", str(e))
            continue

        prompt_continue(f"confirm you see/hear {name} activate, then press Enter to turn OFF")

        print(f"  Turning OFF {name} (GPIO {pin})...")
        try:
            GPIO.output(pin, get_relay_state_gpio(False, terminal, active_low))
        except Exception as e:
            record(f"Relay {name} OFF", "FAIL", str(e))
            continue

        try:
            answer = input(f"  Did {name} activate? [y/n]: ").strip().lower()
        except EOFError:
            answer = 'n'

        if answer == 'y':
            record(f"Relay {name} (GPIO {pin})", "PASS", "user confirmed activation")
        else:
            record(f"Relay {name} (GPIO {pin})", "FAIL", "user reported no activation")

    try:
        GPIO.cleanup()
    except Exception:
        pass


# ============================================================================
# TEST 5: FLOW SENSORS
# ============================================================================

def test_flow_sensors(config):
    section("Test 5/6: Flow Sensors (10-second pulse count)")

    if not GPIO_AVAILABLE:
        record("Flow sensor test", "WARN", "RPi.GPIO not available — skipping")
        return

    inlet_pin  = get_cfg(config, 'waterflow.gpio.flow_sensor_inlet',  17)
    outlet_pin = get_cfg(config, 'waterflow.gpio.flow_sensor_outlet', 27)
    duration   = 10  # seconds

    inlet_count  = [0]
    outlet_count = [0]

    def count_inlet(channel):
        inlet_count[0] += 1

    def count_outlet(channel):
        outlet_count[0] += 1

    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        GPIO.setup(inlet_pin,  GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(outlet_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.add_event_detect(inlet_pin,  GPIO.FALLING, callback=count_inlet)
        GPIO.add_event_detect(outlet_pin, GPIO.FALLING, callback=count_outlet)
    except Exception as e:
        record("Flow sensor GPIO setup", "FAIL", str(e))
        return

    print(f"  Counting pulses on inlet (GPIO {inlet_pin}) and outlet (GPIO {outlet_pin})")
    print(f"  for {duration} seconds — make sure water is flowing...")
    prompt_continue("start flow sensor test")

    inlet_count[0]  = 0
    outlet_count[0] = 0

    for i in range(duration):
        time.sleep(1)
        sys.stdout.write(
            f"\r  {duration - i - 1}s remaining... "
            f"inlet={inlet_count[0]} outlet={outlet_count[0]}    "
        )
        sys.stdout.flush()
    print()

    try:
        GPIO.remove_event_detect(inlet_pin)
        GPIO.remove_event_detect(outlet_pin)
        GPIO.cleanup()
    except Exception:
        pass

    calibration = get_cfg(config, 'waterflow.flow.calibration_factor', 7.5)
    inlet_lpm  = (inlet_count[0]  / calibration) if calibration else 0
    outlet_lpm = (outlet_count[0] / calibration) if calibration else 0

    if inlet_count[0] > 0:
        record(
            f"Flow sensor inlet  (GPIO {inlet_pin})",
            "PASS",
            f"{inlet_count[0]} pulses in {duration}s ≈ {inlet_lpm:.2f} L/min"
        )
    else:
        record(
            f"Flow sensor inlet  (GPIO {inlet_pin})",
            "WARN",
            f"0 pulses — no flow detected (is water running?)"
        )

    if outlet_count[0] > 0:
        record(
            f"Flow sensor outlet (GPIO {outlet_pin})",
            "PASS",
            f"{outlet_count[0]} pulses in {duration}s ≈ {outlet_lpm:.2f} L/min"
        )
    else:
        record(
            f"Flow sensor outlet (GPIO {outlet_pin})",
            "WARN",
            f"0 pulses — no flow detected (is water running?)"
        )


# ============================================================================
# TEST 6: SUMMARY
# ============================================================================

def print_summary():
    section("Test 6/6: Summary")
    print()
    passes   = sum(1 for _, s, _ in results if s == 'PASS')
    warnings = sum(1 for _, s, _ in results if s == 'WARN')
    failures = sum(1 for _, s, _ in results if s == 'FAIL')

    col_w = max(len(name) for name, _, _ in results) + 2 if results else 30

    print(f"  {'Test':<{col_w}}  {'Status':<8}  Detail")
    print("  " + "-" * (col_w + 40))
    for name, status, detail in results:
        if status == 'PASS':
            status_str = green(f"{'PASS':<8}")
        elif status == 'WARN':
            status_str = yellow(f"{'WARN':<8}")
        else:
            status_str = red(f"{'FAIL':<8}")
        detail_str = detail[:60] if detail else ""
        print(f"  {name:<{col_w}}  {status_str}  {detail_str}")

    print()
    print(f"  Results: {green(str(passes) + ' PASS')}  "
          f"{yellow(str(warnings) + ' WARN')}  "
          f"{red(str(failures) + ' FAIL')}")
    print()

    if failures == 0 and warnings == 0:
        print(f"  {green('All tests passed!')}")
    elif failures == 0:
        print(f"  {yellow('All tests passed with warnings. Review WARN items above.')}")
    else:
        print(f"  {red(str(failures) + ' test(s) FAILED. Review FAIL items above.')}")
    print()


# ============================================================================
# MAIN
# ============================================================================

def main():
    print()
    print(bold("=" * 60))
    print(bold("  Solar Hydroponic Monitor — Hardware Test"))
    print(bold("=" * 60))
    print()
    print("  This script tests hardware connectivity interactively.")
    print("  You will be prompted between tests.")
    print()

    config = load_config()

    test_serial_port()
    prompt_continue("I2C / BME280 test")

    test_bme280(config)
    prompt_continue("1-Wire / DS18B20 test")

    test_ds18b20()
    prompt_continue("relay test (WARNING: relays will be activated!)")

    test_relays(config)
    prompt_continue("flow sensor test")

    test_flow_sensors(config)

    print_summary()


if __name__ == '__main__':
    main()
