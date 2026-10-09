#!/usr/bin/env python3
"""
Script 03: Coupled Dyno Torque-Control Test
-------------------------------------------
Hardware: Delta C2000 driving the dyno IM with encoder feedback (IM TQCPG),
          mechanically coupled to the MUT.
Purpose:  verify the coupled dyno can apply predictable constant and ramped
          opposing torque.

The MUT is run separately from its own VFD keypad and sets the shaft speed.
This script only commands dyno torque.

Required C2000 setting:
    Pr.09-03 = 0.0 comm timeout disabled - the script stops talking to the
                   drive while a menu waits for input

Torque direction comes only from the sign of Pr.11-34; RUN is sent without the
FWD/REV bits. With the MUT running forward (positive RPM), negative torque
opposes it (regenerative load) and positive torque assists it (motoring).

The stages (1-5) are listed in print_stages() and shown at startup.

Every exit (normal, Ctrl+C or error) ramps torque to 0 %, writes 0 %, then
sends STOP. Press Ctrl+C a second time to skip the ramp.
"""

import time

import config
from hardware import comms
from hardware.c2000 import C2000
from hardware.encoder import ShaftSpeedEstimator
from utils.console import banner, enter_pressed, log, menu, rule, show_status
from utils.timing import RatePacer

MAX_TEST_TORQUE_PCT = 10.0     # No torque command above this magnitude is accepted
SIGN_TEST_TORQUE_PCT = -2.0    # Stage 3: must oppose a forward-running MUT
RAMP_RATE_PCT_PER_S = 2.0      # Ramp rate for Stage 5 and for shutdown


class CoupledTorqueTest:
    def __init__(self, vfd):
        self.vfd = vfd
        self.pacer = RatePacer(config.LOOP_RATE_HZ)
        self.speed = ShaftSpeedEstimator(vfd.read_encoder_sample())
        self.rpm = None            # raw encoder RPM from the last plausible sample
        self.torque_cmd = 0.0      # last torque command successfully written (%)

    # --------------------------------------------------------------------------
    # Torque commands
    # --------------------------------------------------------------------------
    def write_torque(self, pct):
        pct = round(pct, 1)
        if abs(pct) > MAX_TEST_TORQUE_PCT:
            raise ValueError(f"|torque| must be <= {MAX_TEST_TORQUE_PCT:.1f}% in this test.")
        self.vfd.set_torque(pct)
        self.torque_cmd = pct

    def ramp_to(self, target):
        """Ramps the torque command linearly in elapsed time from its current value to target."""
        start = self.torque_cmd
        duration = abs(target - start) / RAMP_RATE_PCT_PER_S
        t0 = time.monotonic()

        def step():
            frac = min(1.0, (time.monotonic() - t0) / duration) if duration else 1.0
            try:
                self.write_torque(start + (target - start) * frac)
            except Exception as e:
                log(f"[ERROR] Torque write failed: {e}")
                return False       # retried next cycle at the then-current ramp value
            return frac >= 1.0

        self.run_loop(until=step)

    def shutdown(self):
        """Normal shutdown: ramp to 0 %, explicitly write 0 %, STOP."""
        print(f"\n[SHUTDOWN] Ramping torque to 0% at {RAMP_RATE_PCT_PER_S:.1f} %/s "
              f"(Ctrl+C again skips the ramp)...")
        try:
            self.ramp_to(0.0)
        except KeyboardInterrupt:
            print("\n[SHUTDOWN] Ramp skipped.")
        try:
            self.vfd.set_torque(0.0)
            self.torque_cmd = 0.0
            print("[SHUTDOWN] Torque command 0% written.")
        except Exception as e:
            print(f"[ERROR] Writing 0% torque failed: {e}")
        try:
            self.vfd.stop()
            print("[SHUTDOWN] STOP sent.")
        except Exception as e:
            print(f"[ERROR] STOP failed: {e} - use the keypad STOP key.")

    # --------------------------------------------------------------------------
    # Live display
    # --------------------------------------------------------------------------
    def run_loop(self, until):
        """Reads and displays telemetry at LOOP_RATE_HZ until until() returns True."""
        while True:
            self.pacer.wait()
            done = until()
            self.update_display()
            if done:
                print()
                return

    def monitor(self):
        print("Press Enter to continue (Ctrl+C = shutdown).")
        self.run_loop(until=enter_pressed)

    def load_type(self):
        """Quadrant implied by the commanded torque sign vs the shaft direction."""
        if self.torque_cmd == 0 or not self.rpm:
            return "no load"
        return "REGEN" if self.torque_cmd * self.rpm < 0 else "MOTORING"

    def update_display(self):
        try:
            s = self.vfd.read_encoder_sample()
            tel = self.vfd.read_telemetry()
        except Exception as e:
            # The encoder delta over the longer, measured dt stays correct.
            log(f"[ERROR] Telemetry read failed: {e}")
            return

        r = self.speed.update(s)
        if r.plausible:
            self.rpm = r.rpm
        else:
            log(f"[WARN] Implausible encoder jump ({r.rpm:+.0f} rpm). Sample discarded.")
        enc = f"{r.rpm:+7.1f}" if r.plausible else "    ---"

        show_status(
            f"ENC={enc}rpm | Tcmd={self.torque_cmd:+5.1f}% | Test={tel.torque_pct:+5.1f}% "
            f"| {self.load_type():<8} | I={tel.current_a:6.2f}A | P={tel.output_power_kw:5.1f}kW "
            f"| DC={tel.dc_bus_v:5.1f}V | {tel.state} | C2000={s.c2000_rpm:4d}rpm | dt={r.dt * 1000:3.0f}ms"
        )


# ==============================================================================
# STAGES
# ==============================================================================
def print_stages():
    print("\nStages:")
    print(" 1  MUT only           - dyno STOPPED at 0%; start the MUT forward, confirm steady positive ENC")
    print(" 2  Dyno at 0% torque  - RUN at 0% torque; shaft speed should not change")
    print(f" 3  Torque sign        - {SIGN_TEST_TORQUE_PCT:+.1f}% must OPPOSE the forward MUT; if not, zero torque and STOP")
    print(" 4  Constant torque    - step to a signed target and hold")
    print(f" 5  Torque ramp        - ramp to a signed target at {RAMP_RATE_PCT_PER_S:.1f} %/s and hold")
    print(" Exit / Ctrl+C         - ramp to 0%, write 0%, STOP")


def print_legend():
    rule()
    print(" ENC   = encoder shaft RPM (raw, unfiltered); + = forward")
    print(" Tcmd  = torque command written by this script (Pr.11-34, % of Pr.11-27)")
    print(" Test  = C2000 estimated output torque (2208H, %)")
    print(" LOAD  = quadrant implied by Tcmd vs ENC signs (REGEN = opposing the MUT)")
    print(" I / P / DC = output current, output power, DC-bus voltage")
    print(" C2000 = 2207H drive speed (unsigned, comparison only)")
    rule()


def ask_torque():
    """Asks for a signed torque target; returns None if invalid or above the test limit."""
    text = input(f"Target torque in % (signed, |T| <= {MAX_TEST_TORQUE_PCT:.1f}; "
                 f"negative opposes a forward MUT): ").strip()
    try:
        value = float(text)
    except ValueError:
        print("[ERROR] Invalid number.")
        return None
    if abs(value) > MAX_TEST_TORQUE_PCT:
        print(f"[ERROR] Above the {MAX_TEST_TORQUE_PCT:.1f}% test limit (MAX_TEST_TORQUE_PCT).")
        return None
    return value


def torque_sign_test(test):
    """Stage 3. Returns True only if the user confirms the torque opposes the MUT."""
    if not test.rpm or test.rpm <= 0:
        print("[ABORT] Encoder RPM is not positive. The sign test expects the MUT running forward.")
        return False

    print(f"\nSTAGE 3 - Torque sign: ramping to {SIGN_TEST_TORQUE_PCT:+.1f}% "
          f"(must OPPOSE the forward-running MUT).")
    test.ramp_to(SIGN_TEST_TORQUE_PCT)
    print("Opposing (correct): MUT VFD current/power rise on its keypad, RPM may dip slightly.")
    print("Assisting (wrong):  MUT VFD current/power fall, RPM may rise.")
    test.monitor()

    answer = menu("Does the dyno torque OPPOSE the MUT?", [
        ("y", "Yes - it opposes"),
        ("n", "No / unsure - zero torque and STOP now"),
    ])
    if answer == "y":
        return True

    test.write_torque(0.0)
    test.vfd.stop()
    print("[VFD] Torque 0% and STOP sent. The opposite sign is NOT tried automatically - "
          "investigate before re-running.")
    return False


def torque_control(test):
    """Stages 4 and 5: constant and ramped torque, repeated until the user exits."""
    while True:
        choice = menu(f"STAGES 4/5 - Torque control (Tcmd now {test.torque_cmd:+.1f}%)", [
            ("4", "Constant torque: step to a target and hold"),
            ("5", f"Ramp to a target at {RAMP_RATE_PCT_PER_S:.1f} %/s and hold"),
            ("0", "Ramp to 0% and hold"),
            ("m", "Monitor at the current torque"),
            ("q", "Shutdown and exit"),
        ])
        if choice == "q":
            return

        try:
            if choice in ("4", "5"):
                target = ask_torque()
                if target is None:
                    continue
                if choice == "4":
                    test.write_torque(target)
                    print(f"[VFD] Torque command {test.torque_cmd:+.1f}% (step).")
                else:
                    test.ramp_to(target)
                    print(f"[VFD] Ramp complete at {test.torque_cmd:+.1f}%.")
            elif choice == "0":
                test.ramp_to(0.0)
                print("[VFD] Ramp complete at 0%.")
        except Exception as e:
            print(f"[ERROR] Torque command failed: {e}")

        test.monitor()


def run_stages(test):
    print("\nSTAGE 1 - MUT only: dyno STOPPED, torque 0%.")
    test.write_torque(0.0)
    test.vfd.stop()
    print("Start the MUT FORWARD from its own VFD keypad. Confirm ENC is positive and steady.")
    test.monitor()

    if menu("Next", [("2", "Stage 2: RUN dyno at 0% torque"), ("q", "Exit")]) == "q":
        return
    print("\nSTAGE 2 - Dyno enabled at 0% torque.")
    test.write_torque(0.0)
    test.vfd.run()
    print("[VFD] 0% torque written, RUN sent (no FWD/REV bits). Shaft speed should not change.")
    test.monitor()

    if menu("Next", [("3", f"Stage 3: torque sign test at {SIGN_TEST_TORQUE_PCT:+.1f}%"),
                     ("q", "Shutdown and exit")]) == "q":
        return
    if not torque_sign_test(test):
        return

    torque_control(test)


def main():
    banner(
        "Coupled Dyno Torque-Control Test (Script 03)",
        "C2000: torque mode (Pr.00-10 = 2), IM TQCPG (Pr.00-13 = 0), RS-485 torque (Pr.11-33 = 1)",
        comms.describe_link(),
        f"Test torque limit: +/-{MAX_TEST_TORQUE_PCT:.1f}% | Ramp: {RAMP_RATE_PCT_PER_S:.1f} %/s "
        f"| Loop: {config.LOOP_RATE_HZ:.1f} Hz",
    )

    try:
        vfd = C2000.connect()
    except Exception as e:
        print(f"[ERROR] Failed to initialize serial port: {e}")
        return

    try:
        test = CoupledTorqueTest(vfd)    # first encoder read doubles as the comm check
    except Exception as e:
        print(f"[ERROR] Communication check failed: {e}")
        vfd.close()
        return
    print("[VFD] Communication OK.")
    print_stages()
    print_legend()

    try:
        run_stages(test)
    except KeyboardInterrupt:
        print("\n[CTRL+C]")
    except Exception as e:
        print(f"\n[ERROR] {e}")
    finally:
        test.shutdown()
        vfd.close()
        print("[VFD] Serial port closed. Script 03 ended.")


if __name__ == "__main__":
    main()
