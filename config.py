"""
User-editable configuration shared by all test scripts and the final app.

Only values that someone might reasonably change belong here. Fixed hardware
facts (register addresses, command words, parameter ranges) live in
hardware/registers.py.
"""

import math
import os

import serial

# ==============================================================================
# RS-485 / MODBUS RTU LINK TO THE C2000 (must match Pr.09-00 .. Pr.09-04)
# ==============================================================================
PORT_NAME = "/dev/tty.usbserial-A5069RR4"
SLAVE_ADDRESS = 1              # Pr.09-00 Communication Address
BAUDRATE = 38400               # Pr.09-01 Baud Rate
BYTESIZE = 8
PARITY = serial.PARITY_EVEN    # Pr.09-04 = 14 (8,E,1 RTU)
STOPBITS = 1
TIMEOUT_S = 1.0                # Per-transaction serial timeout

# ==============================================================================
# ENCODER (Pr.10-00 = 1 ABZ, Pr.10-01 = 1024, Pr.10-02 = 1 A leads B = FWD)
# ==============================================================================
ENCODER_PPR = 1024                                # Pulses per revolution, per channel
QUADRATURE_FACTOR = 4                             # A/B rising + falling edges
COUNTS_PER_REV = ENCODER_PPR * QUADRATURE_FACTOR  # 4096 quadrature counts per revolution

# Scale of the 32-bit motor actual position (222CH/222DH). Script 02 Test A
# ([XCHK] lines) checks this against 2209H. If it shows POS/PG ~ 0.25, the
# register counts in PPR units -> set to ENCODER_PPR.
POSITION_COUNTS_PER_REV = COUNTS_PER_REV

# First-order low-pass on encoder RPM, alpha = dt / (tau + dt).
# 0.2 s reaches 63 % of a step in 0.2 s - light enough to see accel/decel/coast-down.
SPEED_FILTER_TAU_S = 0.2

# A single sample implying more than this is treated as a register jump (not a
# real speed) and discarded. Shaft speed is bounded by the MUT (2-pole,
# 2925 rpm rated) and Pr.01-00 = 100 Hz (6000 rpm); this leaves margin.
MAX_PLAUSIBLE_RPM = 6000.0

# ==============================================================================
# RUNTIME LOOP
# ==============================================================================
# Nominal sample/control rate (project spec: ~100 ms per cycle). The actual dt
# is always measured, never assumed.
LOOP_RATE_HZ = 10.0

# Torque slew rate used to enter a profile from 0 and to ramp back to 0 on stop.
# 5 N·m/s: full scale (35.56 N·m) in ~7 s, 0.5 N·m per cycle at 10 Hz.
TORQUE_SLEW_NM_PER_S = 5.0

# Consecutive failed cycles (comm errors) tolerated during a run before a
# controlled shutdown is started.
MAX_CONSECUTIVE_COMM_ERRORS = 3

# CSV run logs: one file per run, in a LOG_FOLDER_NAME folder created inside the log
# location. The location is chosen in the GUI ("Browse..."); it starts at
# DEFAULT_LOG_LOCATION, the project root (absolute, whatever folder a script is run from).
LOG_FOLDER_NAME = "logs"
DEFAULT_LOG_LOCATION = os.path.dirname(os.path.abspath(__file__))

# ==============================================================================
# COMMISSIONING LIMITS (runtime)
# ==============================================================================
# Torque scaling, from the dyno motor nameplate (30 kW, 2900 rpm -> 98.79 N·m rated).
# Pr.11-27 is set to 36 %, ie the max commandable torque is 36 % of rated dyno motor
# torque = 35.56 N·m - the max allowed operating torque of our coupled system (see
# reason in documentation).
# The two C2000 torque registers use different bases:
#   Pr.11-34 (command)  % of Pr.11-27:          100 % = TORQUE_REFERENCE_NM
#   2208H    (estimate) % of motor rated torque: 100 % = MOTOR_RATED_TORQUE_NM
# All torque above the drive (profiles, limits, logs, GUI) is in N·m.
# PR_11_27_PCT must match the drive: the runtime reads Pr.11-27 and refuses to start otherwise.
MOTOR_RATED_POWER_KW = 30.0
MOTOR_RATED_RPM = 2900.0
PR_11_27_PCT = 36.0
# Derived - do not edit:
MOTOR_RATED_TORQUE_NM = MOTOR_RATED_POWER_KW * 1000.0 / (2.0 * math.pi * MOTOR_RATED_RPM / 60.0)  # 98.79
TORQUE_REFERENCE_NM = MOTOR_RATED_TORQUE_NM * PR_11_27_PCT / 100.0                                # 35.56

# Every torque command is clamped to [MIN, MAX] N·m.
# With the MUT running forward, negative torque opposes it; MAX = 0 forbids assisting torque.
TORQUE_LIMIT_MIN_NM = -TORQUE_REFERENCE_NM
TORQUE_LIMIT_MAX_NM = 0.0

# Controlled shutdown if |encoder RPM| exceeds this (dyno IM rated 2900 rpm, MUT 
# rated 2925 rpm).
MAX_ABS_RPM = 3000.0

# A run needs the MUT driving the shaft forward: it will not start below this
# encoder RPM, and shuts down if speed falls below it while torque is applied
# (opposing torque on an undriven shaft would spin it backwards).
MIN_RUN_RPM = 100.0
