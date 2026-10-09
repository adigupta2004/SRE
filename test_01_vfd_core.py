#!/usr/bin/env python3
"""
Script 01: Delta C2000 VFD Core Communication & Telemetry Test
----------------------------------------------------------------
Protocol: Modbus RTU over RS-485 via minimalmodbus
Target Hardware: Delta C2000 Series VFD

Interactive menu: RUN / STOP / fault reset, speed or torque target, telemetry.
Pr.00-10 on the keypad must match the control mode selected in this script.

Required C2000 setting:
    Pr.09-03 = 0.0 comm timeout disabled - the script stops talking to the
                   drive while a menu waits for input
"""

import sys

from hardware import comms
from hardware import registers as reg
from hardware.c2000 import C2000
from utils.console import banner, menu, rule


def target_hint(control_mode):
    if control_mode == "SPEED":
        return "(Signed Hz: +FWD / -REV)"
    return "(Signed % Torque Command: +FWD / -REV)"


def print_telemetry(tel):
    rule()
    print(f" Target Freq: {tel.freq_cmd_hz:6.2f} Hz | Actual Output Freq: {tel.output_freq_hz:6.2f} Hz "
          f"| Estimated Motor Speed: {tel.motor_rpm:5d} rpm")
    print(f" Torque: {tel.torque_pct:6.1f} %       | Status: {tel.state:<13}")
    print(f" DC Bus: {tel.dc_bus_v:6.1f} V       | Current: {tel.current_a:7.2f} A            "
          f"| Power: {tel.output_power_kw:6.1f} kW")
    rule()


def main():
    control_mode = "SPEED"        # 'SPEED' or 'TORQUE'
    speed_direction = reg.FWD     # Direction sent with RUN in speed mode

    banner(
        "Delta C2000 Modbus RTU Core Control & Telemetry Test",
        f"Mode: {control_mode} | {comms.describe_link()}",
    )

    try:
        vfd = C2000.connect()
    except Exception as e:
        print(f"[ERROR] Failed to initialize serial port: {e}")
        sys.exit(1)

    try:
        while True:
            choice = menu("Commands", [
                ("1", "Read Telemetry"),
                # Note - FWD or REV mean CW or ACW. The sign convention is the same for both,
                # torque and speed. Their combination determines motoring/regen.
                ("2", f"Set Target {target_hint(control_mode)}"),
                ("3", "Send RUN Command"),
                ("4", "Send STOP Command"),
                ("5", "Reset Fault"),
                ("6", f"Toggle Control Mode in Script (Current: {control_mode})"),
                ("q", "Quit"),
            ])

            try:
                if choice == "1":
                    print_telemetry(vfd.read_telemetry())

                elif choice == "2":
                    val_str = input(f"Enter Target Value {target_hint(control_mode)}: ").strip()
                    try:
                        val = float(val_str)
                    except ValueError:
                        print("[ERROR] Invalid numeric input.")
                        continue

                    if control_mode == "SPEED":
                        # 0 Hz keeps the previous direction
                        if val > 0:
                            speed_direction = reg.FWD
                        elif val < 0:
                            speed_direction = reg.REV
                        raw = vfd.set_frequency(val)
                        print(f"[VFD] Speed Target set to {abs(val):.2f} Hz | Direction: {speed_direction} | Raw: {raw}")
                    else:
                        try:
                            vfd.set_torque(val)
                        except ValueError as e:
                            print(f"[ERROR] {e}")
                            continue
                        print(f"[VFD] Torque Command set to {val:.1f}%")

                elif choice == "3":
                    if control_mode == "SPEED":
                        vfd.run(speed_direction)
                        print(f"[VFD] RUN command sent ({speed_direction}).")
                    else:
                        vfd.run()
                        print("[VFD] RUN command sent.")

                elif choice == "4":
                    vfd.stop()
                    print("[VFD] STOP command sent.")

                elif choice == "5":
                    vfd.reset_fault()
                    print("[VFD] Fault RESET command sent.")

                elif choice == "6":
                    control_mode = "TORQUE" if control_mode == "SPEED" else "SPEED"
                    print(f"[SCRIPT] Script Target Mode updated to: {control_mode}")
                    print("Note: Ensure Pr.00-10 on the VFD keypad matches this selection!")

                elif choice == "q":
                    print("Exiting test script...")
                    break

            except Exception as e:
                # Communication failure on any Modbus transaction: report and keep the menu alive.
                print(f"[ERROR] Modbus transaction failed: {e}")

    finally:
        vfd.close()


if __name__ == "__main__":
    main()