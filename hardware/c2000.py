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
from utils.mathutils import combine_words

POS_WORD_COUNT = reg.REG_POS_HIGH - reg.REG_POS_LOW + 1    # 2 words, one transaction

# 2207H..2209H are contiguous, so C2000 RPM and PG feedback come from one
# transaction. 2208H (output torque) is read as a side effect and ignored.
MONITOR_BLOCK_START = reg.REG_MOTOR_SPEED
MONITOR_BLOCK_COUNT = reg.REG_PG_FEEDBACK - reg.REG_MOTOR_SPEED + 1   # 3 words


@dataclass(frozen=True)
class Telemetry:
    freq_cmd_hz: float
    output_freq_hz: float
    motor_rpm: int          # 2207H, unsigned magnitude (sensorless estimate)
    dc_bus_v: float
    output_power_kw: float
    torque_pct: float       # 2208H, signed
    current_a: float
    status_word: int        # raw 2101H

    @property
    def state(self):
        return reg.DRIVE_STATE_NAMES.get(self.status_word & reg.STATUS_STATE_MASK, "Unknown")


@dataclass(frozen=True)
class EncoderSample:
    t: float                # monotonic time at the midpoint of the position read
    pos_low: int            # 222CH raw
    pos_high: int           # 222DH raw
    pos_raw: int            # unsigned 32-bit combined position
    pg: int                 # 2209H, 0..4095 per rev
    c2000_rpm: int          # 2207H, unsigned (comparison only)


class C2000:
    def __init__(self, instrument):
        self.instrument = instrument

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

    # --------------------------------------------------------------------------
    # Reads
    # --------------------------------------------------------------------------
    def read_output_current(self):
        """Output current in A. 2200H's decimal position is in the high byte of 211FH."""
        raw_current = self._read(reg.REG_OUTPUT_CURR)
        decimal_places = (self._read(reg.REG_CURRENT_DECIMAL) >> 8) & 0xFF
        return raw_current / (10 ** decimal_places)

    def read_telemetry(self):
        """Reads all core operating values (one transaction per register)."""
        return Telemetry(
            freq_cmd_hz=self._read(reg.REG_FREQ_COMMAND_MONITOR, decimals=2),
            output_freq_hz=self._read(reg.REG_OUTPUT_FREQ, decimals=2),
            motor_rpm=self._read(reg.REG_MOTOR_SPEED),
            dc_bus_v=self._read(reg.REG_DC_BUS_VOLT, decimals=1),
            output_power_kw=self._read(reg.REG_OUTPUT_POWER, decimals=1),
            torque_pct=self._read(reg.REG_OUTPUT_TORQUE, decimals=1, signed=True),
            current_a=self.read_output_current(),
            status_word=self._read(reg.REG_DRIVE_STATUS),
        )

    def read_encoder_sample(self):
        """
        Reads the 32-bit encoder position plus 2207H..2209H (two transactions).

        The timestamp is the midpoint of the position transaction, since that is
        the value RPM is calculated from. Any fixed latency inside the transaction
        is the same every sample and cancels out in dt.
        """
        t_start = time.monotonic()
        pos_low, pos_high = self.instrument.read_registers(
            reg.REG_POS_LOW, POS_WORD_COUNT, functioncode=3
        )
        t_end = time.monotonic()

        speed_raw, _torque_raw, pg = self.instrument.read_registers(
            MONITOR_BLOCK_START, MONITOR_BLOCK_COUNT, functioncode=3
        )

        return EncoderSample(
            t=(t_start + t_end) / 2.0,
            pos_low=pos_low,
            pos_high=pos_high,
            pos_raw=combine_words(pos_low, pos_high),
            pg=pg,
            c2000_rpm=speed_raw,
        )
