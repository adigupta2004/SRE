#!/usr/bin/env python3
"""
Script 02: Delta C2000 Encoder Shaft-Speed Commissioning Test (READ-ONLY)
-------------------------------------------------------------------------
Hardware: Delta C2000 + EMC-PG01L (Slot 2) + Baumer EB260F 1024 PPR encoder
Purpose:  establish a trusted, signed mechanical shaft RPM from the encoder.

This script ONLY READS registers. It never sends RUN, STOP, frequency or torque
commands and never writes any parameter.

Registers read:
    222CH / 222DH  Motor actual position (32-bit)  -> encoder RPM
    2209H          PG feedback, 0..4095 per rev    -> scale cross-check
    2207H          C2000 motor speed (rpm)         -> comparison only

The supported tests (A-F) are listed in print_tests() and shown at startup.
Press Ctrl+C to end.
"""

import config
from hardware import comms
from hardware.c2000 import C2000
from hardware.encoder import ShaftSpeedEstimator
from utils.console import banner, log, menu, rule, show_status
from utils.mathutils import to_signed32, wrap_delta
from utils.timing import RatePacer

# Cross-check (Test A): acceptable error in the POS/PG ratio.
# 2209H wraps every rev, so it can only be followed if it moves less than half a
# rev per sample (< 300 rpm at 10 Hz) - turn the ring slowly by hand.
XCHK_TOLERANCE = 0.02

def print_tests():
    print("\nTests:")
    print(" A  Manual ring test     - Mode 1, drive stopped: turn ring slowly by hand; each [XCHK] line should say OK")
    print(" B  Known constant speed - compare ENC_RPM vs the known speed and vs C2000_RPM")
    print(" C  Different speeds     - check scaling at several speeds")
    print(" D  Accel / decel        - ENC_RPM / FILT_RPM track the change with correct sign")
    print(" E  Coast-down           - speed decays naturally toward zero")
    print(" F  Reverse direction    - reverse rotation gives negative RPM")


def xchk_report(pos_total, pg_total):
    """Compares total 32-bit position movement with total PG feedback movement."""
    expected = config.POSITION_COUNTS_PER_REV / config.COUNTS_PER_REV
    ratio = pos_total / pg_total
    verdict = "OK" if abs(ratio - expected) <= XCHK_TOLERANCE * expected else "MISMATCH"
    return (
        f"[XCHK] Shaft {pg_total / config.COUNTS_PER_REV:+.2f} rev (from PG) "
        f"| PG {pg_total:+d} cnt | POS {pos_total:+d} cnt "
        f"| POS/PG = {ratio:+.3f} (expected {expected:+.3f}) -> {verdict}"
    )


def print_legend():
    rule()
    print(" POS       = signed 32-bit motor actual position (222DH:222CH)")
    print(" dCNT      = position change since previous sample")
    print(" PG        = 2209H PG feedback (0..4095 per rev)")
    print(f" ENC_RPM   = dCNT / {config.POSITION_COUNTS_PER_REV} / dt * 60")
    print(f" FILT_RPM  = ENC_RPM low-passed, tau = {config.SPEED_FILTER_TAU_S:.2f} s")
    print(" C2000_RPM = 2207H drive speed estimate (unsigned, comparison only)")
    print(" [XCHK]    = cross-check mode only, printed after each full PG revolution")
    rule()
    print(" Press Ctrl+C to end.\n")


def run_monitor(vfd, first, cross_check):
    speed = ShaftSpeedEstimator(first)
    pacer = RatePacer(config.LOOP_RATE_HZ)
    pos_total = pg_total = 0    # cross-check accumulators

    try:
        while True:
            pacer.wait()
            try:
                s = vfd.read_encoder_sample()
            except Exception as e:
                # Keep the previous sample: the 32-bit delta over the longer,
                # measured dt is still correct once communication recovers.
                log(f"[ERROR] Telemetry read failed: {e}")
                continue

            prev = speed.prev
            r = speed.update(s)
            if not r.plausible:
                log(f"[WARN] Implausible jump: dCNT={r.d_counts:+d} in {r.dt * 1000:.0f} ms "
                    f"({r.rpm:+.0f} rpm). Sample discarded.")
                continue

            if cross_check:
                rev_before = int(pg_total / config.COUNTS_PER_REV)   # truncates toward zero
                pos_total += r.d_counts
                pg_total += wrap_delta(s.pg, prev.pg, config.COUNTS_PER_REV)
                if int(pg_total / config.COUNTS_PER_REV) != rev_before:
                    log(xchk_report(pos_total, pg_total))

            show_status(
                f"POS={to_signed32(s.pos_raw):+11d} | dCNT={r.d_counts:+6d} | PG={s.pg:4d} "
                f"| ENC_RPM={r.rpm:+8.1f} | FILT_RPM={r.rpm_filtered:+8.1f} "
                f"| C2000_RPM={s.c2000_rpm:5d} | dt={r.dt * 1000:4.0f}ms"
            )

    except KeyboardInterrupt:
        print()
        if pg_total != 0:
            print("Cross-check summary:\n  " + xchk_report(pos_total, pg_total))


def main():
    banner(
        "Delta C2000 Encoder Shaft-Speed Test (Script 02)",
        "*** READ-ONLY: no RUN/STOP/frequency/torque commands ***",
        comms.describe_link(),
        f"Encoder: {config.ENCODER_PPR} PPR x{config.QUADRATURE_FACTOR} = {config.COUNTS_PER_REV} counts/rev "
        f"| Sample rate: {config.LOOP_RATE_HZ:.1f} Hz",
    )

    try:
        vfd = C2000.connect()
    except Exception as e:
        print(f"[ERROR] Failed to initialize serial port: {e}")
        return

    try:
        first = vfd.read_encoder_sample()
    except Exception as e:
        print(f"[ERROR] Communication check failed: {e}")
        vfd.close()
        return
    print("[VFD] Communication OK. Drive is NOT being started.")

    try:
        print_tests()
        mode = menu("Modes", [
            ("1", "Scale cross-check + RPM (Test A: drive stopped, turn ring SLOWLY by hand)"),
            ("2", "RPM only                (Tests B-F)"),
        ])
        print_legend()
        run_monitor(vfd, first, cross_check=(mode == "1"))
    except KeyboardInterrupt:
        print()
    finally:
        vfd.close()
        print("[VFD] Serial port closed. Encoder test ended (no commands were sent to the drive).")


if __name__ == "__main__":
    main()
