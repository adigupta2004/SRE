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
# 1. FWD/REV mean CW/ACW in speed control; sign of value in 0x0B22 means
#    CW/ACW in torque control
# 2. When giving a torque command, register 0x0B22 is the only register that 
#    matters. FWD/REV bits of 0x2000 are not read during a torque command, they
#    are only used to indicate direction during speed control.
# 3. Torque and speed share the same sign convention; FWD (speed) and + (torque) 
#    both mean one of CW/ACW, and REV (speed) and - (torque) both mean the other.
#    The combination of torque and speed directions determines motoring vs regen.
REG_TORQUE_TARGET = param_address(11, 34)   # 0x0B22
TORQUE_CMD_MIN_PCT = -100.0
TORQUE_CMD_MAX_PCT = 100.0

# Pr.11-27 Maximum torque command, 0..500 % of motor rated torque (integer %).
# It is the 100 % base of Pr.11-34, so it fixes the command's N·m scaling.
REG_MAX_TORQUE_CMD = param_address(11, 27)  # 0x0B1B

# Induction motor 1 rated power and speed (set by the vendor). The drive's motor
# rated torque = Pr.05-02 / (Pr.05-03 * 2*pi/60) (manual, Pr.11-34) is the 100 %
# base of Pr.11-27 and of the 2208H estimate.
REG_MOTOR_RATED_POWER = param_address(5, 2)  # 0x0502  0.01 kW
REG_MOTOR_RATED_RPM = param_address(5, 3)    # 0x0503  rpm

# ==============================================================================
# STATUS / TELEMETRY REGISTERS (21xx, 22xx, read-only)
# ==============================================================================
REG_DRIVE_STATUS = 0x2101      # 8449  bit1-0 drive state (see DRIVE_STATE_NAMES)
# REG_FREQ_COMMAND_MONITOR holds the same value as REG_FREQ_COMMAND above,
# but is meant for reading while REG_FREQ_COMMAND is meant for writing.
REG_FREQ_COMMAND_MONITOR = 0x2102  # 8450  frequency command, 0.01 Hz
REG_OUTPUT_FREQ = 0x2103       # 8451  actual output frequency, 0.01 Hz
REG_CURRENT_DECIMAL = 0x211F   # 8479  high byte = decimal places of output current

# Output current, output voltage and power angle: together they give the drive's output
# power, sqrt(3) * V * I * cos(angle) (see C2000.Telemetry.power_vi_kw).
REG_OUTPUT_CURR = 0x2200       # 8704  output current (VFD -> motor, AC), scaled by 211FH high byte
REG_DC_BUS_VOLT = 0x2203       # 8707  0.1 V
REG_OUTPUT_VOLT = 0x2204       # 8708  output voltage (VFD -> motor, AC line-line), 0.1 V
# Power angle (verified on the rig, 2026-10-28): signed, 0.1 deg. Its magnitude is the phase
# angle between the output voltage and the output current (0..90 deg; cos of it is the power
# factor); its sign gives the direction of power flow: + motoring (power from the drive into
# the motor), - regenerating (power from the motor into the drive). E.g. +83 deg at 0 N·m
# (drive only supplying the motor's losses), -40 deg at -30 N·m regenerating.
REG_POWER_ANGLE = 0x2205       # 8709  power angle, signed, 0.1 deg
# 2206H (output power, signed 0.1 kW) is deliberately not read: power_vi_kw replaces it
# (reasons in C2000.Telemetry.power_vi_kw).
# Note - estimated speed is not very accurate; becomes 0 as soon as stop command
# is given. So not really estimated speed maybe; somehow related to target? But it
# is not the same as the target for sure. Some estimation is happening. Also, this
# quantity is not a signed number (observed on hardware: magnitude only). Note - 
# this comment is for when we are doing sensorless control.
REG_MOTOR_SPEED = 0x2207       # 8711 motor speed from VFD's calculation, rpm (unsigned)
                               # When sensorless it is estimated from speeds, when TQCPG 
                               # it is probably from encoder itself.
REG_OUTPUT_TORQUE = 0x2208     # 8712  estimated output torque, signed, 0.1 % of MOTOR RATED
                               # torque (not of Pr.11-27 like the command; manual Pr.00-04 = 8).
                               # Currently not a usable torque reading: see C2000.Telemetry.torque_nm.
REG_PG_FEEDBACK = 0x2209       # 8713  PG feedback, 0..4095 per mechanical rev
REG_POS_LOW = 0x222C           # 8748  motor actual position, low word
REG_POS_HIGH = 0x222D          # 8749  motor actual position, high word

STATUS_STATE_MASK = 0x03
DRIVE_STATE_NAMES = {0: "Stopped", 1: "Decelerating", 2: "Standby", 3: "Operating"}
