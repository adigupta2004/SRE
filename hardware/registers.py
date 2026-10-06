"""
Delta C2000 Modbus register map and command words.

Source: C2000 user manual, Chapter 12, Group 09, "4. Address list"
(page 12.1-09-8 onward). Requires Pr.09-30 = 0 (20xx decoding method).

These are fixed hardware facts, not settings - tunable values go in config.py.
"""


def param_address(group, number):
    """Modbus address of drive parameter Pr.GG-nn, e.g. Pr.11-34 -> 0x0B22."""
    return (group << 8) | number


# ==============================================================================
# COMMAND REGISTERS (20xx, read/write)
# ==============================================================================
REG_CMD_RUN_STOP = 0x2000      # 8192  bit1-0 run/stop, bit5-4 direction
REG_FREQ_COMMAND = 0x2001      # 8193  frequency command, 0.01 Hz (write here)
REG_CMD_RESET = 0x2002         # 8194  bit1 = fault reset

CMD_STOP = 0x0001              # bit1-0 = 01b
CMD_RUN = 0x0002               # bit1-0 = 10b, direction unchanged (used in torque mode)
CMD_RUN_FWD = 0x0012           # RUN + bit5-4 = 01b FWD
CMD_RUN_REV = 0x0022           # RUN + bit5-4 = 10b REV
CMD_FAULT_RESET = 0x0002       # 2002H bit1

FWD = "FWD"
REV = "REV"
RUN_CMD_BY_DIRECTION = {FWD: CMD_RUN_FWD, REV: CMD_RUN_REV}

# Pr.11-34 Torque Command, signed, 0.1 % units, relative to Pr.11-27.
# FWD/REV mean CW/ACW; torque and speed share the same sign convention, and
# their combination determines motoring vs regeneration.
REG_TORQUE_TARGET = param_address(11, 34)   # 0x0B22
TORQUE_CMD_MIN_PCT = -100.0
TORQUE_CMD_MAX_PCT = 100.0

# ==============================================================================
# STATUS / TELEMETRY REGISTERS (21xx, 22xx, read-only)
# ==============================================================================
REG_DRIVE_STATUS = 0x2101      # 8449  bit1-0 drive state (see DRIVE_STATE_NAMES)
# REG_FREQ_COMMAND_MONITOR holds the same value as REG_FREQ_COMMAND above,
# but is meant for reading while REG_FREQ_COMMAND is meant for writing.
REG_FREQ_COMMAND_MONITOR = 0x2102  # 8450  frequency command, 0.01 Hz
REG_OUTPUT_FREQ = 0x2103       # 8451  actual output frequency, 0.01 Hz
REG_CURRENT_DECIMAL = 0x211F   # 8479  high byte = decimal places of output current

REG_OUTPUT_CURR = 0x2200       # 8704  output current, scaled by 211FH high byte
REG_DC_BUS_VOLT = 0x2203       # 8707  0.1 V
REG_OUTPUT_POWER = 0x2206      # 8710  0.1 kW
# Note - estimated speed is not very accurate; becomes 0 as soon as stop command
# is given. So not really estimated speed maybe; somehow related to target? But it
# is not the same as the target for sure. Some estimation is happening. Also, this
# quantity is not a signed number (observed on hardware: magnitude only).
REG_MOTOR_SPEED = 0x2207       # 8711  estimated motor speed, rpm (unsigned)
REG_OUTPUT_TORQUE = 0x2208     # 8712  estimated output torque, signed, 0.1 %
REG_PG_FEEDBACK = 0x2209       # 8713  PG feedback, 0..4095 per mechanical rev
REG_POS_LOW = 0x222C           # 8748  motor actual position, low word
REG_POS_HIGH = 0x222D          # 8749  motor actual position, high word

STATUS_STATE_MASK = 0x03
DRIVE_STATE_NAMES = {0: "Stopped", 1: "Decelerating", 2: "Standby", 3: "Operating"}
