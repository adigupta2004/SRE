#!/usr/bin/env python3
"""
Script 02: Delta C2000 Encoder Shaft-Speed Commissioning Test (READ-ONLY)
-------------------------------------------------------------------------
Protocol: Modbus RTU over RS-485 via minimalmodbus (identical settings to Script 01)
Target Hardware: Delta C2000 + EMC-PG01L (Slot 2) + Baumer EB260F 1024 PPR encoder

Purpose: establish a trusted, signed mechanical shaft RPM from the encoder.

This script ONLY READS registers. It never sends RUN, STOP, frequency or torque
commands and never writes any parameter. It does not change the control mode.

Registers read:
    222CH / 222DH  Motor actual position, low / high word (32-bit)  -> primary RPM
    2209H          PG feedback, 0..4095 per rev                     -> diagnostic / scale cross-check
    2207H          C2000 motor speed (rpm)                          -> comparison only (sensorless estimate)

Supported tests (mode selected at startup):
    A  Manual ring test     - cross-check mode: verify 222C/222D is scaled as expected,
                              i.e. 1 rev of 2209H = +4096 POS counts ([XCHK] lines)
    B  Known constant speed - compare ENC_RPM vs known speed and vs C2000_RPM
    C  Different speeds     - check scaling at several speeds
    D  Accel / decel        - ENC_RPM / FILT_RPM track the change with correct sign
    E  Coast-down           - speed decays naturally toward zero
    F  Reverse direction    - reverse rotation gives negative RPM

Press Ctrl+C to end.
"""

import sys
import time
import shutil
from collections import namedtuple

# Reuse Script 01's communication layer unchanged, so both scripts are
# guaranteed to use exactly the same Modbus RTU settings.
from test_01_vfd_core import (
    PORT_NAME,
    SLAVE_ADDRESS,
    BAUDRATE,
    PARITY,
    STOPBITS,
    BYTESIZE,
    TIMEOUT,
    REG_MOTOR_SPEED,   # 2207H
    initialize_vfd,
)

# ==============================================================================
# CONFIGURATION
# ==============================================================================
# Encoder (matches Pr.10-00 = 1 ABZ, Pr.10-01 = 1024, Pr.10-02 = 1 A leads B = FWD)
PPR = 1024                                # Pulses per revolution, per channel
QUADRATURE_FACTOR = 4                     # A/B rising + falling edges
COUNTS_PER_REV = PPR * QUADRATURE_FACTOR  # 4096 quadrature counts per revolution

# Scale ASSUMED for the 32-bit actual position (222C/222D). This is NOT taken
# on trust: the [XCHK] cross-check against 2209H reports whether it is correct.
# If [XCHK] shows POS/PG ~ 0.25, the register counts in PPR units -> set to PPR.
POSITION_COUNTS_PER_REV = COUNTS_PER_REV

# Telemetry registers
REG_PG_FEEDBACK = 0x2209       # 8713 (PG feedback, 0..4095 per mechanical rev)
REG_POS_LOW = 0x222C           # 8748 (Motor actual position, low word)
REG_POS_HIGH = 0x222D          # 8749 (Motor actual position, high word)
POS_WORD_COUNT = REG_POS_HIGH - REG_POS_LOW + 1          # 2 words, one transaction

# 2207H..2209H are contiguous, so C2000 RPM and PG feedback come from one
# transaction. 2208H (output torque) is read as a side effect and ignored.
MONITOR_BLOCK_START = REG_MOTOR_SPEED
MONITOR_BLOCK_COUNT = REG_PG_FEEDBACK - REG_MOTOR_SPEED + 1  # 3 words

# Sampling
SAMPLE_RATE_HZ = 10.0          # Nominal loop rate; actual dt is always measured

# Filtering: first-order low-pass, alpha = dt / (tau + dt).
# 0.2 s reaches 63 % of a step in 0.2 s - light enough to see accel/decel/coast-down.
FILTER_TAU_S = 0.2

# Sanity limit: a single sample implying more than this is treated as a register
# jump (not a real speed) and reported instead of being displayed as RPM.
# The motor is a 2-pole 11 kW machine (~3000 rpm); this leaves generous margin.
MAX_PLAUSIBLE_RPM = 6000.0

# Scale cross-check (POS vs unwrapped 2209H), selected at startup for Test A.
# 2209H can only be unwrapped reliably if it moves less than half a rev per
# sample (< 300 rpm at 10 Hz), so the ring must be turned slowly by hand.
XCHK_MIN_PG_COUNTS = COUNTS_PER_REV // 4    # PG movement needed before judging a static POS
XCHK_TOLERANCE = 0.02                       # Acceptable POS/PG ratio error (2 %)

Sample = namedtuple("Sample", "t pos_low pos_high pos_raw pg c2000_rpm")


# ==============================================================================
# POSITION / COUNTER ARITHMETIC
# ==============================================================================
# Interpretation of the 32-bit position:
#   Delta transfers 32-bit values as two 16-bit registers, low word at the lower
#   address (222CH) and high word at the next address (222DH). The combined value
#   is a free-running two's-complement counter that wraps modulo 2^32.
#
#   We keep the UNSIGNED bit pattern (0 .. 2^32-1) for arithmetic and only
#   convert to signed for display. Speed uses the signed modulo-2^32 difference
#   between consecutive samples. This is equivalent to unsigned modulo-2^32 
#   difference -  just that the rollover boundary in unsigned is at 0, while
#   in signed it is at the edge between +ve and -ve. 
#   Note - doing modulo is what handles rolloverin either direction properly, in 
#   signed as well as unsigned. However, there is an assumption - this is correct 
#   only as long as the shaft moves less than half the counter size between samples.
#   In this case, that limit becomes 2^31 counts between samples (2^31/4096 revolutions in 0.1s,
#   which is physically impossible here). Reason - think of it in terms of an unsigned
#   counter, as that is easier to visualise - two readings can always be explained by more 
#   than one movement on the circle. Take the bigger arc, smaller arc or integer revolutions
#   plus some arc. We assume the smallest movement - ie the smaller arc. That represents
#   reality only when the motion was less than half the circumference.

def combine_position(low_word, high_word):
    """
    Combines 222CH (low) and 222DH (high) into an unsigned 32-bit pattern.
    Both words must be unsigned 0..65535, as returned by read_registers().
    """
    return (high_word << 16) | low_word


def to_signed32(raw):
    """Two's-complement interpretation of an unsigned 32-bit pattern (for display)."""
    return raw - 2**32 if raw >= 2**31 else raw


def delta_signed32(new_raw, old_raw):
    """Signed shortest distance from old to new on the 32-bit counter ring."""
    forward = (new_raw - old_raw) % 2**32     # distance going forward round the ring: 0 .. 2^32-1
    if forward >= 2**31:                      # more than half way round forward ->
        return forward - 2**32                # the shorter path is backward (negative)
    return forward


def delta_pg(new_pg, old_pg):
    """
    Signed shortest distance on the unsigned 0..4095 PG feedback ring.
    Only valid for slow movement (< half a rev per sample). Used ONLY for the
    scale cross-check, never for RPM.
    """
    forward = (new_pg - old_pg) % COUNTS_PER_REV
    if forward >= COUNTS_PER_REV // 2:
        return forward - COUNTS_PER_REV
    return forward


# ==============================================================================
# MODBUS READS
# ==============================================================================
def read_sample(vfd):
    """
    Reads one telemetry sample. Raises on communication failure.

    The timestamp is the midpoint of the position transaction, since that is
    the value RPM is calculated from. Any fixed latency inside the transaction
    is the same every sample and cancels out in dt.
    """
    t_start = time.monotonic()
    pos_low, pos_high = vfd.read_registers(REG_POS_LOW, POS_WORD_COUNT, functioncode=3)
    t_end = time.monotonic()

    speed_raw, _torque_raw, pg = vfd.read_registers(
        MONITOR_BLOCK_START, MONITOR_BLOCK_COUNT, functioncode=3
    )

    return Sample(
        t=(t_start + t_end) / 2.0,
        pos_low=pos_low,
        pos_high=pos_high,
        pos_raw=combine_position(pos_low, pos_high),
        pg=pg,
        # Raw unsigned value, read the same way as Script 01 (which observed
        # 2207H as unsigned, i.e. speed magnitude without direction).
        c2000_rpm=speed_raw,
    )


# ==============================================================================
# DISPLAY
# ==============================================================================
def show_status(line):
    """Overwrites the single status line, truncated so it never wraps."""
    width = shutil.get_terminal_size((120, 24)).columns - 1
    print("\r\x1b[K" + line[:width], end="", flush=True)


def log(message):
    """Prints a persistent message above the status line."""
    print("\r\x1b[K" + message, flush=True)


def format_status(s, d_cnt, enc_rpm, filt_rpm, dt):
    return (
        f"PG={s.pg:4d} | 222D:222C={s.pos_high:04X}:{s.pos_low:04X} "
        f"| POS={to_signed32(s.pos_raw):+11d} | dCNT={d_cnt:+6d} "
        f"| ENC_RPM={enc_rpm:+8.1f} | FILT_RPM={filt_rpm:+8.1f} "
        f"| C2000_RPM={s.c2000_rpm:5d} | dt={dt * 1000:4.0f}ms"
    )


def xchk_report(pos_total, pg_total):
    """One-line comparison of 32-bit position movement vs PG feedback movement."""
    expected = POSITION_COUNTS_PER_REV / COUNTS_PER_REV
    ratio = pos_total / pg_total
    verdict = "OK" if abs(ratio - expected) <= XCHK_TOLERANCE * abs(expected) else "MISMATCH"
    return (
        f"[XCHK] Shaft {pg_total / COUNTS_PER_REV:+.2f} rev (from PG) "
        f"| PG {pg_total:+d} cnt | POS {pos_total:+d} cnt "
        f"| POS/PG = {ratio:+.3f} (expected {expected:+.3f}) -> {verdict}"
    )


def print_snapshot(s):
    print("-" * 75)
    print(" Initial raw values")
    print(f"  2209H PG feedback      : {s.pg:5d}  (0x{s.pg:04X})")
    print(f"  222CH position low     : {s.pos_low:5d}  (0x{s.pos_low:04X})")
    print(f"  222DH position high    : {s.pos_high:5d}  (0x{s.pos_high:04X})")
    print(f"  32-bit position        : {to_signed32(s.pos_raw):+d}  (0x{s.pos_raw:08X})")
    print(f"  2207H C2000 speed      : {s.c2000_rpm:d} rpm  (raw unsigned, sensorless estimate, comparison only)")
    print("-" * 75)


def print_legend():
    print(" PG        = 2209H raw PG feedback (0..4095 per rev, diagnostic only)")
    print(" 222D:222C = raw high:low position words (hex)")
    print(" POS       = signed 32-bit motor actual position")
    print(" dCNT      = signed position change since previous sample")
    print(f" ENC_RPM   = dCNT / {POSITION_COUNTS_PER_REV} / dt * 60   (raw encoder RPM)")
    print(f" FILT_RPM  = ENC_RPM through first-order low-pass, tau = {FILTER_TAU_S:.2f} s")
    print(" C2000_RPM = 2207H drive speed estimate (comparison only)")
    print(" [XCHK]    = cross-check mode only, printed after each full PG revolution:")
    print("             checks that 1 rev of 2209H equals 4096 position counts")
    print("-" * 75)
    print(" Press Ctrl+C to end.\n")


# ==============================================================================
# MONITOR LOOP
# ==============================================================================
def run_monitor(vfd, first, cross_check):
    period = 1.0 / SAMPLE_RATE_HZ
    prev = first
    filt_rpm = None

    # Scale cross-check accumulators (used only when cross_check is True)
    xchk_pos_total = 0
    xchk_pg_total = 0
    xchk_last_rev = 0
    xchk_static_warned = False

    next_t = time.monotonic()
    try:
        while True:
            # Simple pacing: aim for SAMPLE_RATE_HZ, never try to catch up.
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()

            try:
                s = read_sample(vfd)
            except Exception as e:
                # Keep 'prev': the 32-bit delta over a longer, measured dt
                # is still correct once communication recovers.
                log(f"[ERROR] Telemetry read failed: {e}")
                continue

            dt = s.t - prev.t

            # --- Encoder RPM from the 32-bit position ---
            d_cnt = delta_signed32(s.pos_raw, prev.pos_raw)
            enc_rpm = (d_cnt / POSITION_COUNTS_PER_REV) / dt * 60.0

            if abs(enc_rpm) > MAX_PLAUSIBLE_RPM:
                log(
                    f"[WARN] Implausible position jump: dCNT={d_cnt:+d} in {dt * 1000:.0f} ms "
                    f"({enc_rpm:+.0f} rpm) | 222D:222C {prev.pos_high:04X}:{prev.pos_low:04X} "
                    f"-> {s.pos_high:04X}:{s.pos_low:04X} | PG {prev.pg} -> {s.pg}. Sample discarded."
                )
                prev = s
                continue

            if filt_rpm is None:
                filt_rpm = enc_rpm
            else:
                alpha = dt / (FILTER_TAU_S + dt)
                filt_rpm += alpha * (enc_rpm - filt_rpm)

            # --- Scale cross-check against 2209H (operator turns ring slowly) ---
            if cross_check:
                xchk_pos_total += d_cnt
                xchk_pg_total += delta_pg(s.pg, prev.pg)

                if (not xchk_static_warned and xchk_pos_total == 0
                        and abs(xchk_pg_total) >= XCHK_MIN_PG_COUNTS):
                    log(
                        f"[XCHK][WARN] 2209H moved {xchk_pg_total:+d} counts but 222C/222D "
                        f"did not change. 32-bit position is NOT tracking the encoder."
                    )
                    xchk_static_warned = True

                rev = int(xchk_pg_total / COUNTS_PER_REV)  # truncates toward zero
                if rev != xchk_last_rev:
                    xchk_last_rev = rev
                    log(xchk_report(xchk_pos_total, xchk_pg_total))

            show_status(format_status(s, d_cnt, enc_rpm, filt_rpm, dt))
            prev = s

    except KeyboardInterrupt:
        print()
        if xchk_pg_total != 0:
            print("Cross-check summary:")
            print("  " + xchk_report(xchk_pos_total, xchk_pg_total))


def main():
    print("=======================================================")
    print(" Delta C2000 Encoder Shaft-Speed Test (Script 02)")
    print(" *** READ-ONLY: no RUN/STOP/frequency/torque commands ***")
    print(f" Port: {PORT_NAME} | Slave: {SLAVE_ADDRESS} | Baud: {BAUDRATE} "
          f"| Format: {BYTESIZE}{PARITY}{STOPBITS} | Timeout: {TIMEOUT:.1f} s")
    print(f" Encoder: {PPR} PPR x{QUADRATURE_FACTOR} = {COUNTS_PER_REV} counts/rev "
          f"| Assumed position scale: {POSITION_COUNTS_PER_REV} counts/rev")
    print(f" Sample rate: {SAMPLE_RATE_HZ:.1f} Hz (actual dt measured)")
    print("=======================================================")

    vfd = initialize_vfd(PORT_NAME, SLAVE_ADDRESS)

    try:
        try:
            first = read_sample(vfd)
        except Exception as e:
            print(f"[ERROR] Communication check failed: {e}")
            sys.exit(1)

        print("[VFD] Communication OK. Drive is NOT being started.")
        print_snapshot(first)

        print("Modes:")
        print(" [1] Scale cross-check + RPM (Test A: drive stopped, turn ring SLOWLY by hand)")
        print(" [2] RPM only                (Tests B-F)")
        choice = ""
        while choice not in ("1", "2"):
            choice = input("\nSelect Option > ").strip()
        cross_check = choice == "1"

        print_legend()
        run_monitor(vfd, first, cross_check)

    except KeyboardInterrupt:
        print()
    finally:
        vfd.serial.close()
        print("[VFD] Serial port closed. Encoder test ended (no commands were sent to the drive).")


if __name__ == "__main__":
    main()
