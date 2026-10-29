#!/usr/bin/env python3
"""
Regenerative Dynamometer - the dyno software
--------------------------------------------
Hardware: Delta C2000 (IM TQCPG, torque from RS-485) driving the dyno IM,
          coupled to the MUT, which is run separately from its own keypad.
Purpose:  run the dyno from one window: choose a torque profile, run it in the ~10 Hz
          runtime (control/runtime.py) with live plots, log every run to CSV, and view
          the results (commanded vs estimated torque and its error statistics) for this
          or any older run, always ending with a clean torque -> 0 -> STOP shutdown.
          Without a drive connected the window opens as a log viewer only.

This file only wires the pieces together, in two processes:
    control process:  C2000 (hardware) -> DynoRuntime (control)   the 10 Hz loop + CSV log
    this process:     RuntimeProcess (control) <-> DynoWindow (gui)
The control process is started by RuntimeProcess and owns the serial port; this
process runs only the GUI, so plotting can never slow the loop down (see
control/runtime_process.py).

Flow: connect -> 0 N·m + STOP -> check C2000 torque settings matches config (torque scaling)
-> start the MUT forward and watch the live RPM -> choose a profile -> Start
(0 N·m + RUN, slew into the profile) -> run until Stop or profile end -> ramp to
0, write 0 N·m, STOP -> results shown in the window. Torque is in N·m throughout.
Limits are in config.py (OPERATING LIMITS). Ctrl+C closes the window,
which stops any active run through the same shutdown path.
"""

import signal
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import config
from control.runtime_process import RuntimeProcess
from gui.dyno_window import DynoWindow
from hardware import comms
from utils.console import banner


def main():
    banner(
        "Regenerative Dynamometer",
        comms.describe_link(),
        f"Loop {config.LOOP_RATE_HZ:g} Hz | Torque {config.TORQUE_LIMIT_MIN_NM:.2f} .. "
        f"{config.TORQUE_LIMIT_MAX_NM:.2f} N·m | |RPM| <= {config.MAX_ABS_RPM:g} | "
        f"Slew {config.TORQUE_SLEW_NM_PER_S:g} N·m/s",
    )

    # Start the control process first: it opens the port, sends 0 N·m + STOP, checks
    # the torque scaling and starts the loop in IDLE. If that fails (drive off, wrong
    # port, scaling mismatch), the window still opens, as a log viewer only.
    print("[VFD] Starting the control process ...")
    runtime, startup_error = RuntimeProcess(), None
    try:
        runtime.start()
        print(f"[VFD] 0 N·m and STOP sent. Torque scaling verified: Pr.05-02 = {config.MOTOR_RATED_POWER_KW:g} kW, "
              f"Pr.05-03 = {config.MOTOR_RATED_RPM:g} rpm, Pr.11-27 = {config.PR_11_27_PCT:g} %.")
        print("[VFD] Start the MUT forward, then use the window.")
    except Exception as e:
        runtime, startup_error = None, str(e)
        print(f"[ERROR] Runtime not started: {e}")
        print("[VFD] Opening the window as a log viewer only.")

    app = QApplication(sys.argv)
    window = DynoWindow(runtime, startup_error)   # plugs its handlers into the runtime's callbacks

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
        # through the shutdown path if a run is somehow active, then the control
        # process closes the serial port and exits. Its last messages go to the terminal.
        if runtime is not None:
            runtime.on_message = lambda text: print(f"[RUNTIME] {text}")
            if runtime.close():
                print("[VFD] Runtime stopped cleanly; serial port closed.")
            else:
                print("[ERROR] Runtime did not finish its shutdown - check the drive and use keypad STOP.")
        print("[VFD] Dyno software ended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
