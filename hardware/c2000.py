"""
Delta C2000 driver for the dyno (loading) induction motor.

Every Modbus read/write to the C2000 goes through the C2000 class, so register
encodings live in exactly one place. Methods raise on communication failure
(minimalmodbus / serial exceptions) and never print: callers decide whether to
log and continue, retry or stop.

Control mode (Pr.00-10 speed/torque) is set on the keypad, not by software.
"""

import time
from dataclasses import dataclass

import config
from hardware import comms
from hardware import registers as reg
from utils.mathutils import combine_words, to_signed16

# Block reads. Each Modbus transaction costs ~20 ms whatever its size (C2000 RTU
# frames are delimited by >= 10 ms silent intervals, plus the Pr.09-09 response
# delay), so contiguous registers are read together. The manual does not state the
# maximum registers per read clearly ("n <= 16" data); 10-word reads are verified on
# the rig (2026-10-25).
POS_WORD_COUNT = reg.REG_POS_HIGH - reg.REG_POS_LOW + 1    # 222CH..222DH, 2 words

# 2207H..2209H: C2000 RPM and PG feedback for read_encoder_sample(). 2208H (output
# torque) is read as a side effect and ignored.
MONITOR_BLOCK_START = reg.REG_MOTOR_SPEED
MONITOR_BLOCK_COUNT = reg.REG_PG_FEEDBACK - reg.REG_MOTOR_SPEED + 1    # 3 words

# 2101H..2103H: drive status, frequency command, output frequency.
STATUS_BLOCK_START = reg.REG_DRIVE_STATUS
STATUS_BLOCK_COUNT = reg.REG_OUTPUT_FREQ - reg.REG_DRIVE_STATUS + 1    # 3 words

# 2200H..2209H: current, DC bus, output power, C2000 RPM, output torque, PG feedback
# (2201H, 2202H, 2204H and 2205H come along unused).
DRIVE_BLOCK_START = reg.REG_OUTPUT_CURR
DRIVE_BLOCK_COUNT = reg.REG_PG_FEEDBACK - reg.REG_OUTPUT_CURR + 1      # 10 words


@dataclass(frozen=True)
class Telemetry:
    freq_cmd_hz: float
    output_freq_hz: float
    motor_rpm: int          # 2207H, unsigned magnitude (sensorless estimate)
    dc_bus_v: float
    output_power_kw: float  # 2206H, signed (negative = regenerating)
    torque_pct: float       # 2208H, signed, % of motor rated torque
    current_a: float
    status_word: int        # raw 2101H

    @property
    def state(self):
        return reg.DRIVE_STATE_NAMES.get(self.status_word & reg.STATUS_STATE_MASK, "Unknown")

    @property
    def torque_nm(self):
        """2208H in N·m (its 100 % is the motor rated torque, not Pr.11-27)."""
        return self.torque_pct / 100.0 * config.MOTOR_RATED_TORQUE_NM


@dataclass(frozen=True)
class EncoderSample:
    t: float                # monotonic time at the midpoint of the position read
    pos_raw: int            # 222CH (low) / 222DH (high) combined: unsigned 32-bit position
    pg: int                 # 2209H, 0..4095 per rev
    c2000_rpm: int          # 2207H, unsigned (comparison only)


class C2000:
    def __init__(self, instrument):
        self.instrument = instrument
        self._current_divisor = None    # 10 ** (2200H decimal places), read once from 211FH

    @classmethod
    def connect(cls, port=config.PORT_NAME, slave_address=config.SLAVE_ADDRESS):
        return cls(comms.open_instrument(port, slave_address))

    def close(self):
        self.instrument.serial.close()

    # --------------------------------------------------------------------------
    # Low-level register access
    # --------------------------------------------------------------------------
    def _write(self, address, value, decimals=0, signed=False):
        self.instrument.write_register(
            address, value, number_of_decimals=decimals, functioncode=6, signed=signed
        )

    def _read(self, address, decimals=0, signed=False):
        return self.instrument.read_register(
            address, number_of_decimals=decimals, functioncode=3, signed=signed
        )

    def _read_block(self, start, count):
        """Reads count contiguous registers in one transaction: {address: unsigned raw word}."""
        words = self.instrument.read_registers(start, count, functioncode=3)
        return dict(zip(range(start, start + count), words))

    # --------------------------------------------------------------------------
    # Commands
    # --------------------------------------------------------------------------
    def run(self, direction=None):
        """
        Sends RUN. With direction (reg.FWD / reg.REV) the direction bits are set
        too (speed mode); with None the direction is left unchanged (torque mode).
        """
        command = reg.CMD_RUN if direction is None else reg.RUN_CMD_BY_DIRECTION[direction]
        self._write(reg.REG_CMD_RUN_STOP, command)

    def stop(self):
        self._write(reg.REG_CMD_RUN_STOP, reg.CMD_STOP)

    def reset_fault(self):
        self._write(reg.REG_CMD_RESET, reg.CMD_FAULT_RESET)

    def set_frequency(self, hz):
        """
        Writes the speed-mode frequency command magnitude to 2001H (0.01 Hz).
        Direction is not part of this register - it is set by run(direction).
        Returns the raw value written.
        """
        raw = int(abs(hz) * 100)
        self._write(reg.REG_FREQ_COMMAND, raw)
        return raw

    def set_torque(self, pct):
        """Writes the signed torque command Pr.11-34 (% of Pr.11-27, 0.1 % units)."""
        if not reg.TORQUE_CMD_MIN_PCT <= pct <= reg.TORQUE_CMD_MAX_PCT:
            raise ValueError(
                f"Torque command must be between {reg.TORQUE_CMD_MIN_PCT:+.1f}% "
                f"and {reg.TORQUE_CMD_MAX_PCT:+.1f}%."
            )
        self._write(reg.REG_TORQUE_TARGET, pct, decimals=1, signed=True)

    def set_torque_nm(self, nm):
        """
        Writes a torque command in N·m: converted to Pr.11-34 (% of Pr.11-27,
        100 % = config.TORQUE_REFERENCE_NM) and rounded to its 0.1 % resolution
        (~0.036 N·m). Returns the N·m actually commanded after that rounding.
        """
        pct = round(nm / config.TORQUE_REFERENCE_NM * 100.0, 1) + 0.0     # + 0.0: -0.0 -> 0.0
        self.set_torque(pct)
        return pct / 100.0 * config.TORQUE_REFERENCE_NM

    def verify_torque_scaling(self):
        """
        Raises ValueError unless the drive parameters that define the N·m scaling
        match config: Pr.05-02 / Pr.05-03 (motor rated power / speed -> rated
        torque, the 100 % of 2208H) and Pr.11-27 (the 100 % of the Pr.11-34
        command). A mismatch would make every N·m command and reading silently
        wrong. All mismatches are listed in one message.
        """
        checks = [
            ("Pr.05-02 motor rated power", self._read(reg.REG_MOTOR_RATED_POWER, decimals=2),
             "MOTOR_RATED_POWER_KW", config.MOTOR_RATED_POWER_KW, "kW"),
            ("Pr.05-03 motor rated speed", self._read(reg.REG_MOTOR_RATED_RPM),
             "MOTOR_RATED_RPM", config.MOTOR_RATED_RPM, "rpm"),
            ("Pr.11-27 max torque command", self._read(reg.REG_MAX_TORQUE_CMD),
             "PR_11_27_PCT", config.PR_11_27_PCT, "%"),
        ]
        mismatches = [
            f"{name}: drive {actual:g} {unit}, config.{key} {expected:g} {unit}"
            for name, actual, key, expected, unit in checks
            if abs(actual - expected) > 0.005          # 0.01 kW is the finest register step
        ]
        if mismatches:
            raise ValueError("Torque scaling mismatch - fix the drive or config.py before running. "
                             + "; ".join(mismatches))

    # --------------------------------------------------------------------------
    # Reads
    # --------------------------------------------------------------------------
    def read_cycle(self):
        """
        Everything the runtime needs per cycle in three transactions: encoder
        position, the 2200H..2209H block and the 2101H..2103H block.
        Returns (EncoderSample, Telemetry).
        """
        t, pos_raw = self._read_position()
        drive = self._read_block(DRIVE_BLOCK_START, DRIVE_BLOCK_COUNT)
        status = self._read_block(STATUS_BLOCK_START, STATUS_BLOCK_COUNT)
        return self._encoder_sample(t, pos_raw, drive), self._telemetry(status, drive)

    def read_telemetry(self):
        """Reads all core operating values (two block transactions)."""
        status = self._read_block(STATUS_BLOCK_START, STATUS_BLOCK_COUNT)
        drive = self._read_block(DRIVE_BLOCK_START, DRIVE_BLOCK_COUNT)
        return self._telemetry(status, drive)

    def read_encoder_sample(self):
        """Reads the 32-bit encoder position plus 2207H..2209H (two transactions)."""
        t, pos_raw = self._read_position()
        monitor = self._read_block(MONITOR_BLOCK_START, MONITOR_BLOCK_COUNT)
        return self._encoder_sample(t, pos_raw, monitor)

    # --------------------------------------------------------------------------
    # Read helpers: one transaction each, then pure decoding of the raw words
    # --------------------------------------------------------------------------
    def _read_position(self):
        """
        Reads 222CH..222DH in one transaction (so the two halves are consistent).
        Returns (t, pos_raw): the unsigned 32-bit position and t = midpoint of the
        transaction, since that is the value RPM is calculated from. Any fixed
        latency inside the transaction is the same every sample and cancels out in dt.
        """
        t_start = time.monotonic()
        pos_low, pos_high = self.instrument.read_registers(
            reg.REG_POS_LOW, POS_WORD_COUNT, functioncode=3
        )
        t_end = time.monotonic()
        return (t_start + t_end) / 2.0, combine_words(pos_low, pos_high)

    def _get_current_divisor(self):
        """2200H's decimal places (high byte of 211FH) are fixed for a drive model: read once."""
        if self._current_divisor is None:
            decimal_places = (self._read(reg.REG_CURRENT_DECIMAL) >> 8) & 0xFF
            self._current_divisor = 10 ** decimal_places
        return self._current_divisor

    @staticmethod
    def _encoder_sample(t, pos_raw, words):
        """words: a block containing 2207H (C2000 RPM) and 2209H (PG feedback)."""
        return EncoderSample(
            t=t,
            pos_raw=pos_raw,
            pg=words[reg.REG_PG_FEEDBACK],
            c2000_rpm=words[reg.REG_MOTOR_SPEED],
        )

    def _telemetry(self, status, drive):
        """Decodes the 2101H..2103H (status) and 2200H..2209H (drive) blocks."""
        return Telemetry(
            freq_cmd_hz=status[reg.REG_FREQ_COMMAND_MONITOR] / 100.0,
            output_freq_hz=status[reg.REG_OUTPUT_FREQ] / 100.0,
            motor_rpm=drive[reg.REG_MOTOR_SPEED],
            dc_bus_v=drive[reg.REG_DC_BUS_VOLT] / 10.0,
            output_power_kw=to_signed16(drive[reg.REG_OUTPUT_POWER]) / 10.0,
            torque_pct=to_signed16(drive[reg.REG_OUTPUT_TORQUE]) / 10.0,
            current_a=drive[reg.REG_OUTPUT_CURR] / self._get_current_divisor(),
            status_word=status[reg.REG_DRIVE_STATUS],
        )
