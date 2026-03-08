#!/usr/bin/env python3

"""
Extended Renogy Rover MODBUS Interface
Extends the renogymodbus.RenogyChargeController class with additional functions
based on the official Renogy MODBUS protocol documentation.

This class adds support for:
- Daily statistics (min/max voltage, current, power, amp-hours)
- Historical data (operating days, over-discharges, full-charges)
- Cumulative statistics (total amp-hours, power generation/consumption)
- Fault and warning information (detailed error codes)
- Improved error handling and retry logic
- Batch Modbus reads for efficiency (improvement #7)
"""

from renogymodbus import RenogyChargeController
import time
import logging

class RenogyRoverExtended(RenogyChargeController):
    """
    Extended Renogy Rover class with additional MODBUS register support.

    Inherits from renogymodbus.RenogyChargeController and adds methods for:
    - Daily battery statistics
    - Historical operational data
    - Cumulative energy statistics
    - Detailed fault/warning information
    - Enhanced error handling
    - Batch register reads for fewer bus transactions per poll cycle

    All new methods follow the naming convention and implementation style
    of the parent renogymodbus library.
    """

    def __init__(self, *args, max_retries=3, retry_delay=1.0, **kwargs):
        """
        Initialize with retry configuration.

        Args:
            max_retries: Maximum number of retry attempts for failed reads (default: 3)
            retry_delay: Delay in seconds between retries (default: 1.0)
        """
        super().__init__(*args, **kwargs)
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.read_errors = 0
        self.total_reads = 0

        # Batch-read cache: populated by batch_read(), consumed by individual getters
        self._batch_cache = {}           # {register_address: value}
        self._batch_timestamp = 0.0      # epoch seconds of last batch_read()
        self._batch_ttl = 12.0           # seconds before cache is considered stale

    # ========================================================================
    # RETRY INFRASTRUCTURE
    # ========================================================================

    def read_register_with_retry(self, register_address):
        """
        Read a single register with retry logic.

        Args:
            register_address: MODBUS register address

        Returns:
            Register value or None on failure
        """
        # Check batch cache first
        cached = self._get_from_cache(register_address)
        if cached is not None:
            return cached

        self.total_reads += 1

        for attempt in range(self.max_retries):
            try:
                value = self.read_register(register_address)
                return value
            except Exception as e:
                if attempt < self.max_retries - 1:
                    logging.warning(
                        f"Read register 0x{register_address:04X} failed "
                        f"(attempt {attempt+1}/{self.max_retries}): {e}"
                    )
                    time.sleep(self.retry_delay * (attempt + 1))
                else:
                    self.read_errors += 1
                    logging.error(
                        f"Read register 0x{register_address:04X} failed "
                        f"after {self.max_retries} attempts: {e}"
                    )
                    raise
        return None

    def read_registers_with_retry(self, start_address, count):
        """
        Read multiple registers with retry logic.

        Args:
            start_address: Starting MODBUS register address
            count: Number of registers to read

        Returns:
            List of register values or None on failure
        """
        self.total_reads += 1

        for attempt in range(self.max_retries):
            try:
                values = self.read_registers(start_address, count)
                return values
            except Exception as e:
                if attempt < self.max_retries - 1:
                    logging.warning(
                        f"Read registers 0x{start_address:04X}:{count} failed "
                        f"(attempt {attempt+1}/{self.max_retries}): {e}"
                    )
                    time.sleep(self.retry_delay * (attempt + 1))
                else:
                    self.read_errors += 1
                    logging.error(
                        f"Read registers 0x{start_address:04X}:{count} failed "
                        f"after {self.max_retries} attempts: {e}"
                    )
                    raise
        return None

    def get_error_rate(self):
        """Get the current error rate for diagnostics."""
        if self.total_reads == 0:
            return 0.0
        return (self.read_errors / self.total_reads) * 100

    # ========================================================================
    # BATCH READ CACHE (improvement #7)
    # ========================================================================

    def _get_from_cache(self, register_address):
        """
        Return cached register value if the cache is fresh, else None.
        Called internally by read_register_with_retry.
        """
        if not self._batch_cache:
            return None
        age = time.time() - self._batch_timestamp
        if age > self._batch_ttl:
            return None
        return self._batch_cache.get(register_address)  # None if not cached

    def _store_registers(self, start_address, values):
        """Store a list of register values starting at start_address into cache."""
        for i, val in enumerate(values):
            self._batch_cache[start_address + i] = val

    def batch_read(self):
        """
        Read all frequently-used registers in 3 Modbus calls instead of ~25.

        Reads:
          - 0x0100–0x011F  (32 regs): real-time data + daily stats + historical
          - 0x0120–0x0122  ( 3 regs): load status + faults
          - 0x011A–0x011F  (already covered above by the first block)

        The individual getter methods will use the cached data automatically
        for the remainder of the poll cycle (up to _batch_ttl seconds).

        Returns:
            True if all reads succeeded; False if any failed (partial cache may
            still be populated and is better than nothing).
        """
        self._batch_cache = {}
        self._batch_timestamp = time.time()

        success = True

        # Block 1: 0x0100–0x011F (real-time data, daily stats, historical, cumulative)
        try:
            regs = self.read_registers_with_retry(0x0100, 0x0020)  # 32 registers
            if regs is not None:
                self._store_registers(0x0100, regs)
            else:
                success = False
        except Exception as e:
            logging.warning(f"batch_read block 0x0100–0x011F failed: {e}")
            success = False

        # Block 2: 0x0120–0x0122 (load status + faults, 3 registers)
        try:
            regs = self.read_registers_with_retry(0x0120, 3)
            if regs is not None:
                self._store_registers(0x0120, regs)
            else:
                success = False
        except Exception as e:
            logging.warning(f"batch_read block 0x0120–0x0122 failed: {e}")
            success = False

        return success

    def invalidate_batch_cache(self):
        """Force expiry of the batch cache (e.g., at start of new poll cycle)."""
        self._batch_cache = {}
        self._batch_timestamp = 0.0

    # ========================================================================
    # REAL-TIME BATTERY METRICS
    # ========================================================================

    def get_battery_charging_current(self):
        """
        Get the charging current flowing into the battery from the controller output.

        This is distinct from get_solar_current() (reg 0x0108), which measures
        the current coming from the solar panel into the controller input.

        Returns:
            float: Battery charging current in amps (A)

        Register: 0x0102
        Formula: value * 0.01
        """
        register = self.read_register_with_retry(0x0102)
        return register * 0.01 if register is not None else None

    # ========================================================================
    # DAILY STATISTICS (Current Day)
    # ========================================================================

    def get_daily_min_battery_voltage(self):
        """
        Get the minimum battery voltage recorded today.

        Returns:
            float: Minimum battery voltage in volts (V)

        Register: 0x010B
        Formula: value * 0.1
        """
        register = self.read_register_with_retry(0x010B)
        return register * 0.1 if register is not None else None

    def get_daily_max_battery_voltage(self):
        """
        Get the maximum battery voltage recorded today.

        Returns:
            float: Maximum battery voltage in volts (V)

        Register: 0x010C
        Formula: value * 0.1
        """
        register = self.read_register_with_retry(0x010C)
        return register * 0.1 if register is not None else None

    def get_daily_max_charging_current(self):
        """
        Get the maximum charging current recorded today.

        Returns:
            float: Maximum charging current in amps (A)

        Register: 0x010D
        Formula: value * 0.01
        """
        register = self.read_register_with_retry(0x010D)
        return register * 0.01 if register is not None else None

    def get_daily_max_discharging_current(self):
        """
        Get the maximum discharging current recorded today.

        Returns:
            float: Maximum discharging current in amps (A)

        Register: 0x010E
        Formula: value * 0.01
        """
        register = self.read_register_with_retry(0x010E)
        return register * 0.01 if register is not None else None

    def get_daily_max_charging_power(self):
        """
        Get the maximum charging power recorded today.

        Returns:
            int: Maximum charging power in watts (W)

        Register: 0x010F
        """
        return self.read_register_with_retry(0x010F)

    def get_daily_max_discharging_power(self):
        """
        Get the maximum discharging power recorded today.

        Returns:
            int: Maximum discharging power in watts (W)

        Register: 0x0110
        """
        return self.read_register_with_retry(0x0110)

    def get_daily_charging_ah(self):
        """
        Get the total charging amp-hours for today.

        Returns:
            int: Charging amp-hours (Ah)

        Register: 0x0111
        """
        return self.read_register_with_retry(0x0111)

    def get_daily_discharging_ah(self):
        """
        Get the total discharging amp-hours for today.

        Returns:
            int: Discharging amp-hours (Ah)

        Register: 0x0112
        """
        return self.read_register_with_retry(0x0112)

    def get_daily_power_generation(self):
        """
        Get the total power generation for today.

        Returns:
            float: Power generation in kilowatt-hours (kWh)

        Register: 0x0113
        Formula: value / 10000 (stored as kWh * 10000)
        """
        register = self.read_register_with_retry(0x0113)
        return register / 10000.0 if register is not None else None

    def get_daily_power_consumption(self):
        """
        Get the total power consumption for today.

        Returns:
            float: Power consumption in kilowatt-hours (kWh)

        Register: 0x0114
        Formula: value / 10000 (stored as kWh * 10000)
        """
        register = self.read_register_with_retry(0x0114)
        return register / 10000.0 if register is not None else None

    # ========================================================================
    # HISTORICAL DATA
    # ========================================================================

    def get_total_operating_days(self):
        """
        Get the total number of days the controller has been operating.

        Returns:
            int: Number of operating days

        Register: 0x0115
        """
        return self.read_register_with_retry(0x0115)

    def get_total_battery_over_discharges(self):
        """
        Get the total number of times the battery has been over-discharged.

        Returns:
            int: Number of over-discharge events

        Register: 0x0116
        """
        return self.read_register_with_retry(0x0116)

    def get_total_battery_full_charges(self):
        """
        Get the total number of times the battery has been fully charged.

        Returns:
            int: Number of full charge events

        Register: 0x0117
        """
        return self.read_register_with_retry(0x0117)

    # ========================================================================
    # CUMULATIVE STATISTICS (Lifetime Totals)
    # ========================================================================

    def get_total_charging_ah(self):
        """
        Get the total cumulative charging amp-hours (lifetime).

        Returns:
            int: Total charging amp-hours (Ah)

        Registers: 0x0118-0x0119 (4 bytes / DWORD)
        """
        registers = self.read_registers_with_retry(0x0118, 2)
        if registers is None:
            return None
        value = (registers[0] << 16) | registers[1]
        return value

    def get_total_discharging_ah(self):
        """
        Get the total cumulative discharging amp-hours (lifetime).

        Returns:
            int: Total discharging amp-hours (Ah)

        Registers: 0x011A-0x011B (4 bytes / DWORD)
        """
        registers = self.read_registers_with_retry(0x011A, 2)
        if registers is None:
            return None
        value = (registers[0] << 16) | registers[1]
        return value

    def get_cumulative_power_generation(self):
        """
        Get the total cumulative power generation (lifetime).

        Returns:
            float: Total power generation in kilowatt-hours (kWh)

        Registers: 0x011C-0x011D (4 bytes / DWORD)
        Formula: value / 100 (stored as kWh * 100)
        """
        registers = self.read_registers_with_retry(0x011C, 2)
        if registers is None:
            return None
        value = (registers[0] << 16) | registers[1]
        return value / 100.0

    def get_cumulative_power_consumption(self):
        """
        Get the total cumulative power consumption (lifetime).

        Returns:
            float: Total power consumption in kilowatt-hours (kWh)

        Registers: 0x011E-0x011F (4 bytes / DWORD)
        Formula: value / 100 (stored as kWh * 100)
        """
        registers = self.read_registers_with_retry(0x011E, 2)
        if registers is None:
            return None
        value = (registers[0] << 16) | registers[1]
        return value / 100.0

    # ========================================================================
    # LOAD/STREET LIGHT STATUS
    # ========================================================================

    def get_load_status(self):
        """
        Get the load (street light) status and brightness.

        Returns:
            dict: {
                'is_on': bool,
                'brightness': int (0-100),
                'charging_state': str
            }

        Register: 0x0120
        High byte bit 7: load on/off
        High byte bits 0-6: brightness (0-100%)
        Low byte: charging state code
        """
        register = self.read_register_with_retry(0x0120)
        if register is None:
            return None

        high_byte = (register >> 8) & 0xFF
        low_byte = register & 0xFF

        is_on = bool(high_byte & 0x80)
        brightness = high_byte & 0x7F

        charging_states = {
            0x00: 'deactivated',
            0x01: 'activated',
            0x02: 'mppt',
            0x03: 'equalizing',
            0x04: 'boost',
            0x05: 'floating',
            0x06: 'current_limiting'
        }
        charging_state = charging_states.get(low_byte, 'unknown')

        return {
            'is_on': is_on,
            'brightness': brightness,
            'charging_state': charging_state
        }

    # ========================================================================
    # FAULT AND WARNING INFORMATION
    # ========================================================================

    def get_faults_and_warnings(self):
        """
        Get detailed fault and warning information from the controller.

        Returns:
            dict: Dictionary of fault/warning flags with boolean values

        Registers: 0x0121-0x0122 (4 bytes / DWORD)
        Each bit represents a specific fault or warning condition.
        """
        registers = self.read_registers_with_retry(0x0121, 2)
        if registers is None:
            return {}

        fault_word = (registers[0] << 16) | registers[1]

        faults = {
            'charge_mos_short_circuit': bool(fault_word & (1 << 30)),
            'anti_reverse_mos_short': bool(fault_word & (1 << 29)),
            'solar_panel_reversed': bool(fault_word & (1 << 28)),
            'solar_panel_working_point_over_voltage': bool(fault_word & (1 << 27)),
            'solar_panel_counter_current': bool(fault_word & (1 << 26)),
            'pv_input_over_voltage': bool(fault_word & (1 << 25)),
            'pv_input_short_circuit': bool(fault_word & (1 << 24)),
            'pv_input_over_power': bool(fault_word & (1 << 23)),
            'ambient_temp_too_high': bool(fault_word & (1 << 22)),
            'controller_temp_too_high': bool(fault_word & (1 << 21)),
            'load_over_power_or_over_current': bool(fault_word & (1 << 20)),
            'load_short_circuit': bool(fault_word & (1 << 19)),
            'battery_under_voltage_warning': bool(fault_word & (1 << 18)),
            'battery_over_voltage': bool(fault_word & (1 << 17)),
            'battery_over_discharge': bool(fault_word & (1 << 16)),
        }

        return faults

    def get_active_faults(self):
        """
        Get a list of currently active faults and warnings.

        Returns:
            list: List of active fault names (strings)
        """
        all_faults = self.get_faults_and_warnings()
        active = [name for name, is_active in all_faults.items() if is_active]
        return active

    # ========================================================================
    # COMPREHENSIVE STATUS METHOD
    # ========================================================================

    def get_all_statistics(self):
        """
        Get all available statistics in a single call.

        Returns:
            dict: Comprehensive dictionary containing all available data
        """
        try:
            stats = {
                'daily': {
                    'min_battery_voltage': self.get_daily_min_battery_voltage(),
                    'max_battery_voltage': self.get_daily_max_battery_voltage(),
                    'max_charging_current': self.get_daily_max_charging_current(),
                    'max_discharging_current': self.get_daily_max_discharging_current(),
                    'max_charging_power': self.get_daily_max_charging_power(),
                    'max_discharging_power': self.get_daily_max_discharging_power(),
                    'charging_ah': self.get_daily_charging_ah(),
                    'discharging_ah': self.get_daily_discharging_ah(),
                    'power_generation_kwh': self.get_daily_power_generation(),
                    'power_consumption_kwh': self.get_daily_power_consumption(),
                },
                'historical': {
                    'operating_days': self.get_total_operating_days(),
                    'battery_over_discharges': self.get_total_battery_over_discharges(),
                    'battery_full_charges': self.get_total_battery_full_charges(),
                },
                'cumulative': {
                    'charging_ah': self.get_total_charging_ah(),
                    'discharging_ah': self.get_total_discharging_ah(),
                    'power_generation_kwh': self.get_cumulative_power_generation(),
                    'power_consumption_kwh': self.get_cumulative_power_consumption(),
                },
                'load': self.get_load_status(),
                'faults': self.get_faults_and_warnings(),
                'active_faults': self.get_active_faults(),
            }
            return stats
        except Exception as e:
            return {'error': str(e)}


# ============================================================================
# USAGE EXAMPLE
# ============================================================================

if __name__ == "__main__":
    rover = RenogyRoverExtended(port='/dev/serial0', baudrate=9600)

    # Efficient: do one batch read per poll cycle
    rover.batch_read()

    print("=== Base Class Methods ===")
    print(f"Battery Voltage: {rover.get_battery_voltage():.2f}V")
    print()

    print("=== Extended Methods - Daily Stats ===")
    print(f"Daily Min Voltage: {rover.get_daily_min_battery_voltage():.2f}V")
    print(f"Daily Max Voltage: {rover.get_daily_max_battery_voltage():.2f}V")
    print(f"Daily Power Generation: {rover.get_daily_power_generation():.4f} kWh")
    print()

    print("=== Extended Methods - Historical ===")
    print(f"Operating Days: {rover.get_total_operating_days()}")
    print(f"Over-Discharges: {rover.get_total_battery_over_discharges()}")
    print(f"Full Charges: {rover.get_total_battery_full_charges()}")
    print()

    print("=== Extended Methods - Cumulative ===")
    print(f"Total Charging: {rover.get_total_charging_ah()} Ah")
    print(f"Total Power Generation: {rover.get_cumulative_power_generation():.2f} kWh")
    print()

    print("=== Extended Methods - Load Status ===")
    load_status = rover.get_load_status()
    print(f"Load On: {load_status['is_on']}")
    print(f"Brightness: {load_status['brightness']}%")
    print(f"Charging State: {load_status['charging_state']}")
    print()

    print("=== Extended Methods - Faults ===")
    active_faults = rover.get_active_faults()
    if active_faults:
        print("Active faults:")
        for fault in active_faults:
            print(f"  - {fault}")
    else:
        print("No active faults")
    print()

    print("=== Communication Statistics ===")
    print(f"Total reads: {rover.total_reads}")
    print(f"Failed reads: {rover.read_errors}")
    print(f"Error rate: {rover.get_error_rate():.2f}%")
