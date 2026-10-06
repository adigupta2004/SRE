"""
User-editable configuration shared by all test scripts and the final app.

Only values that someone might reasonably change belong here. Fixed hardware
facts (register addresses, command words, parameter ranges) live in
hardware/registers.py.
"""

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
# 2940 rpm rated) and Pr.01-00 = 100 Hz (6000 rpm); this leaves margin.
MAX_PLAUSIBLE_RPM = 6000.0

# ==============================================================================
# RUNTIME LOOP
# ==============================================================================
# Nominal sample/control rate (project spec: ~100 ms per cycle). The actual dt
# is always measured, never assumed.
LOOP_RATE_HZ = 10.0
