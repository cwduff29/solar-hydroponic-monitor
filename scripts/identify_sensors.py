#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
identify_sensors.py - DS18B20 sensor identification helper

Usage: python3 scripts/identify_sensors.py

Discovers all DS18B20 sensors connected via 1-Wire and displays their IDs and
current temperatures. Refreshes every 5 seconds. Place ONE sensor at a time in
each location to identify which ID corresponds to which logical name.

No external dependencies — stdlib only.
"""

import os
import sys
import time
import glob


DS18B20_BASE_DIR    = '/sys/bus/w1/devices/'
DS18B20_PATTERN     = DS18B20_BASE_DIR + '28-*'
REFRESH_INTERVAL    = 5  # seconds


def read_temperature(sensor_path):
    """
    Read temperature from a DS18B20 sensor.

    Returns (celsius: float, fahrenheit: float) or (None, None) on failure.
    """
    try:
        w1_file = os.path.join(sensor_path, 'w1_slave')
        with open(w1_file, 'r') as f:
            lines = f.readlines()
        if len(lines) < 2:
            return None, None
        if 'YES' not in lines[0]:
            return None, None
        eq_pos = lines[1].find('t=')
        if eq_pos == -1:
            return None, None
        raw = int(lines[1][eq_pos + 2:].strip())
        celsius = raw / 1000.0
        fahrenheit = celsius * 9.0 / 5.0 + 32.0
        return celsius, fahrenheit
    except Exception:
        return None, None


def discover_sensors():
    """Return list of (sensor_id, full_path) tuples."""
    folders = sorted(glob.glob(DS18B20_PATTERN))
    sensors = []
    for folder in folders:
        sensor_id = os.path.basename(folder)
        sensors.append((sensor_id, folder))
    return sensors


def print_table(sensors_data):
    """Print a formatted table of sensor ID → temperature."""
    print()
    print("  {:<24}  {:>10}  {:>10}".format("Sensor ID", "Temp (°C)", "Temp (°F)"))
    print("  " + "-" * 50)
    for sensor_id, celsius, fahrenheit in sensors_data:
        if celsius is not None:
            print("  {:<24}  {:>9.2f}°  {:>9.2f}°".format(
                sensor_id, celsius, fahrenheit))
        else:
            print("  {:<24}  {:>10}  {:>10}".format(
                sensor_id, "ERROR", "ERROR"))
    print()


def print_config_snippet(sensor_ids):
    """Print a ready-to-paste config.json snippet for sensor_map."""
    print()
    print("  Ready-to-paste config.json snippet:")
    print("  ------------------------------------")
    print('  "sensor_map": {')
    for i, sensor_id in enumerate(sensor_ids):
        comma = "," if i < len(sensor_ids) - 1 else ""
        print(f'    "{sensor_id}": "name_{i + 1}"{comma}  // TODO: replace name_{i + 1}')
    print('  }')
    print()
    print("  Example names: reservoir, nft_drain, outdoor")
    print()


def main():
    print()
    print("=" * 60)
    print("  DS18B20 Sensor Identification Tool")
    print("=" * 60)
    print()
    print("  Instructions:")
    print("  1. Connect your sensors one at a time to the 1-Wire bus.")
    print("  2. Note the sensor ID shown when only ONE sensor is connected.")
    print("  3. That ID belongs to the sensor in that location.")
    print("  4. Repeat for each sensor location.")
    print()
    print("  Place only ONE sensor at a time in each location to identify it.")
    print()
    print("  Press Ctrl+C to exit and see the config.json snippet.")
    print()

    seen_ids = set()

    try:
        while True:
            os.system('clear')
            print()
            print("=" * 60)
            print("  DS18B20 Sensor Identification Tool")
            print("=" * 60)
            print()
            print("  Place only ONE sensor at a time in each location to")
            print("  identify it. Note the sensor ID shown.")
            print()
            print(f"  Last scan: {time.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"  Refreshing every {REFRESH_INTERVAL}s — Ctrl+C to exit")

            sensors = discover_sensors()

            if not sensors:
                print()
                print("  No DS18B20 sensors found.")
                print("  Check that 1-Wire is enabled (raspi-config → Interfaces → 1-Wire).")
                print("  Check wiring: VCC=3.3V, GND, DATA with 4.7kΩ pull-up to VCC.")
                print()
            else:
                sensors_data = []
                for sensor_id, folder in sensors:
                    celsius, fahrenheit = read_temperature(folder)
                    sensors_data.append((sensor_id, celsius, fahrenheit))
                    seen_ids.add(sensor_id)

                print(f"  Found {len(sensors)} sensor(s):")
                print_table(sensors_data)

            time.sleep(REFRESH_INTERVAL)

    except KeyboardInterrupt:
        print()
        print()
        print("=" * 60)
        print("  Sensor identification complete.")
        print("=" * 60)

        all_ids = sorted(seen_ids)
        if all_ids:
            print(f"  Total unique sensors seen: {len(all_ids)}")
            print()
            print_config_snippet(all_ids)
            print("  Add the sensor_map snippet to:")
            print("    config.json → waterflow → temperature → sensor_map")
        else:
            print("  No sensors were detected during this session.")
        print()
        sys.exit(0)


if __name__ == '__main__':
    main()
