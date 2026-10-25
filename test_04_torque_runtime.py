#!/usr/bin/env python3
"""
Script 04: Torque Runtime, Profiles and Torque-Feedback Validation
------------------------------------------------------------------
Hardware: Delta C2000 (IM TQCPG, torque from RS-485) driving the dyno IM,
          coupled to the MUT, which is run separately from its own keypad.
Purpose:  prove the reusable ~10 Hz runtime (control/runtime.py), torque profiles
          (control/torque_profiles.py), CSV logging, live plots and the
          commanded vs C2000-estimated torque comparison, with a clean
          torque -> 0 -> STOP shutdown.

This script only wires the pieces together:
    C2000 (hardware) -> DynoRuntime (control) -> DynoWindow (gui)

Flow: connect -> 0 N·m + STOP -> check C2000 torque settings matches config (torque scaling)
-> start the MUT forward and watch the live RPM -> choose a profile -> Start
(0 N·m + RUN, slew into the profile) -> run until Stop or profile end -> ramp to
0, write 0 N·m, STOP -> results shown in the window. Torque is in N·m throughout.
Limits are in config.py (COMMISSIONING LIMITS). Ctrl+C closes the window,
which stops any active run through the same shutdown path.
"""

import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import config
from control.runtime import DynoRuntime
from gui.dyno_window import DynoWindow
from hardware import comms
from hardware.c2000 import C2000
from utils.console import banner


def main():
    banner(
        "Torque Runtime & Validation Test (Script 04)",
        comms.describe_link(),
        f"Loop {config.LOOP_RATE_HZ:g} Hz | Torque {config.TORQUE_LIMIT_MIN_NM:.2f} .. "
        f"{config.TORQUE_LIMIT_MAX_NM:.2f} N·m | |RPM| <= {config.MAX_ABS_RPM:g} | "
        f"Slew {config.TORQUE_SLEW_NM_PER_S:g} N·m/s",
    )

    try:
        vfd = C2000.connect()
        vfd.read_encoder_sample()            # communication check
    except Exception as e:
        print(f"[ERROR] Could not communicate with the C2000: {e}")
        return 1

    app = QApplication(sys.argv)
    runtime = DynoRuntime(vfd)
    window = DynoWindow(runtime)             # connects the runtime's callbacks to the GUI

    try:
        runtime.start()                      # 0 N·m + STOP, scaling check, then IDLE monitoring
    except Exception as e:
        print(f"[ERROR] Runtime not started: {e}")
        vfd.close()
        return 1
    print(f"[VFD] 0 N·m and STOP sent. Torque scaling verified: Pr.05-02 = {config.MOTOR_RATED_POWER_KW:g} kW, "
          f"Pr.05-03 = {config.MOTOR_RATED_RPM:g} rpm, Pr.11-27 = {config.PR_11_27_PCT:g} %.")
    print("[VFD] Start the MUT forward, then use the window.")

    # Ctrl+C in the terminal -> close the window -> controlled shutdown.
    # Python only runs signal handlers when it gets control back, and Qt's event loop
    # (C++) normally keeps it, so a do-nothing timer hands control to Python every
    # 200 ms; without it Ctrl+C would be ignored until the next GUI event.
    signal.signal(signal.SIGINT, lambda *_: window.close())
    keep_python_responsive = QTimer()
    keep_python_responsive.start(200)
    keep_python_responsive.timeout.connect(lambda: None)

    window.show()
    try:
        app.exec()
    finally:
        # Window closed (normally only after any run has stopped). close() still goes
        # through the shutdown path if a run is somehow active, then ends the worker.
        if runtime.close():
            print("[VFD] Runtime stopped cleanly.")
        else:
            print("[ERROR] Runtime did not finish its shutdown - check the drive and use keypad STOP.")
        vfd.close()
        print("[VFD] Serial port closed. Script 04 ended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
