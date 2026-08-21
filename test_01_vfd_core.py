#!/usr/bin/env python3
"""
Script 01: Delta C2000 VFD Core Communication & Telemetry Test
----------------------------------------------------------------
Protocol: Modbus RTU over RS-485 via minimalmodbus
Target Hardware: Delta C2000 Series VFD
"""

import sys
import serial
import minimalmodbus

# ==============================================================================
# CONFIGURATION
# ==============================================================================
PORT_NAME = "/dev/tty.usbserial-A5069RR4"  # Bottom left port
SLAVE_ADDRESS = 1          # Pr.09-00 Communication Address
BAUDRATE = 38400            # Pr.09-01 Baud Rate
PARITY = serial.PARITY_EVEN  # Match Pr.09-04 setting
STOPBITS = 1               # Match Pr.09-04 setting (8E1)
BYTESIZE = 8
TIMEOUT = 1.0              # Seconds

# Operating Mode Flag for Target Dispatch ('SPEED' or 'TORQUE')
CONTROL_MODE = "SPEED"
SPEED_DIRECTION = "FWD"
# Command macros
CMD_RUN = 0x0002
CMD_RUN_FWD = 0x0012
CMD_RUN_REV = 0x0022
CMD_STOP = 0x0001

# Modbus Register Addresses (Hex)
REG_CMD_RUN_STOP = 0x2000     # 8192
REG_SPEED_TARGET  = 0x2001     # 8193
REG_CMD_RESET     = 0x2002     # 8194
REG_TORQUE_TARGET = 0x0B22     # Pr.11-34 Torque Command (-100.0% to +100.0%)

REG_OUTPUT_FREQ   = 0x2103     # 8451 (0.01 Hz)
REG_DRIVE_STATUS  = 0x2101     # 8449
REG_OUTPUT_CURR   = 0x2200     # 8704 (Output current)
REG_CURRENT_DECIMAL = 0x211F   # 8479 (High byte gives decimal position for current)
REG_DC_BUS_VOLT   = 0x2203     # 8707 (0.1 V)
REG_OUTPUT_POWER  = 0x2206     # 8710 (0.1 kW)
REG_OUTPUT_TORQUE = 0x2208     # 8712 (Estimated signed output torque (%))


def initialize_vfd(port, slave_addr):
    """Initializes and configures the minimalmodbus instrument."""
    try:
        instrument = minimalmodbus.Instrument(port, slave_addr)
        instrument.serial.baudrate = BAUDRATE
        instrument.serial.bytesize = BYTESIZE
        instrument.serial.parity = PARITY
        instrument.serial.stopbits = STOPBITS
        instrument.serial.timeout = TIMEOUT
        instrument.mode = minimalmodbus.MODE_RTU
        instrument.clear_buffers_before_each_transaction = True
        return instrument
    except Exception as e:
        print(f"[ERROR] Failed to initialize serial port {port}: {e}")
        sys.exit(1)

def send_run_command(vfd):
    """Sends the RUN command to register 2000H"""
    try:
        if CONTROL_MODE == "SPEED":
            command = CMD_RUN_FWD if SPEED_DIRECTION == "FWD" else CMD_RUN_REV
        else:
            command = CMD_RUN
        vfd.write_register(REG_CMD_RUN_STOP, command, number_of_decimals=0, functioncode=6)

        print(f"[VFD] RUN command sent"
              f"{f' ({SPEED_DIRECTION})' if CONTROL_MODE == 'SPEED' else ''}.")
    except Exception as e:
        print(f"[ERROR] Failed to send RUN command: {e}")


def send_stop_command(vfd):
    """Sends the STOP command to register 2000H (Value = 0x0001)."""
    try:
        vfd.write_register(REG_CMD_RUN_STOP, 1, number_of_decimals=0, functioncode=6)
        print("[VFD] STOP command sent.")
    except Exception as e:
        print(f"[ERROR] Failed to send STOP command: {e}")


def send_fault_reset(vfd):
    """Sends a Fault Reset command to register 2002H (Value = 0x0002)."""
    try:
        vfd.write_register(REG_CMD_RESET, 2, number_of_decimals=0, functioncode=6)
        print("[VFD] Fault RESET command sent.")
    except Exception as e:
        print(f"[ERROR] Failed to send Fault Reset command: {e}")


def set_target_value(vfd, mode, val):
    """
    Sets the Speed or Torque target based on the selected mode.

    - Speed Mode Target:
        Frequency command in Hz, written to 2001H in 0.01 Hz increments.

    - Torque Mode Target:
        Pr.11-34 Torque Command in %, written in 0.1% increments.
        Valid range: -100.0% to +100.0%.
    """
    global SPEED_DIRECTION
    try:
        if mode == "SPEED":
            if val > 0:
                SPEED_DIRECTION = "FWD"
            elif val < 0:
                SPEED_DIRECTION = "REV"
            raw_val = int(abs(val) * 100)
            vfd.write_register(
                REG_SPEED_TARGET,
                raw_val,
                number_of_decimals=0,
                functioncode=6
            )
            print(
                f"[VFD] Speed Target set to {abs(val):.2f} Hz "
                f"| Direction: {SPEED_DIRECTION} | Raw: {raw_val}"
            )

        elif mode == "TORQUE":
            if not -100.0 <= val <= 100.0:
                print("[ERROR] Torque command must be between -100.0% and +100.0%.")
                return
            vfd.write_register(
                REG_TORQUE_TARGET,
                val,
                number_of_decimals=1,
                functioncode=6,
                signed=True
            )
            print(f"[VFD] Torque Command set to {val:.1f}%")

    except Exception as e:
        print(f"[ERROR] Failed to set target value: {e}")


def read_telemetry(vfd):
    """Reads and prints all core operating parameters from the VFD."""
    try:
        freq_hz = vfd.read_register(REG_OUTPUT_FREQ, number_of_decimals=2, functioncode=3)
        dc_v    = vfd.read_register(REG_DC_BUS_VOLT, number_of_decimals=1, functioncode=3)
        pwr_kw  = vfd.read_register(REG_OUTPUT_POWER, number_of_decimals=1, functioncode=3)
        torque_pct = vfd.read_register(REG_OUTPUT_TORQUE, number_of_decimals=1, functioncode=3, signed=True)

        # Read raw current without MinimalModbus scaling it
        raw_current  = vfd.read_register(REG_OUTPUT_CURR, number_of_decimals=0, functioncode=3)
        # Read register that tells us the current decimal position
        decimal_reg = vfd.read_register(REG_CURRENT_DECIMAL, number_of_decimals=0, functioncode=3)
        # Manual says the HIGH BYTE contains the decimal information
        decimal_places = (decimal_reg >> 8) & 0xFF
        curr_a = raw_current / (10 ** decimal_places)

        status  = vfd.read_register(REG_DRIVE_STATUS, number_of_decimals=0, functioncode=3)
        # Status bits decoding (Bits 0-1)
        status_state = status & 0x03
        state_str = {0: "Stopped", 1: "Decelerating", 2: "Standby", 3: "Operating"}.get(status_state, "Unknown")

        print("-" * 55)
        print(f" Freq: {freq_hz:6.2f} Hz | Torque: {torque_pct:6.1f} % | Status: {state_str:<13} | Status Reg: 0x{status:04X} ")
        print(f" DC Bus: {dc_v:6.1f} V   | Current: {curr_a:7.2f} A    | Power: {pwr_kw:6.1f} kW ")
        print("-" * 55)
    except Exception as e:
        print(f"[ERROR] Telemetry read failed: {e}")


def main():
    global CONTROL_MODE
    print("=======================================================")
    print(" Delta C2000 Modbus RTU Core Control & Telemetry Test")
    print(f" Mode: {CONTROL_MODE} | Port: {PORT_NAME} | Baud: {BAUDRATE}")
    print("=======================================================")

    vfd = initialize_vfd(PORT_NAME, SLAVE_ADDRESS)

    while True:
        print("\nCommands:")
        print(" [1] Read Telemetry")
        # Note - FWD or REV mean CW or ACW. The sign convention is the same for both, torque and speed. Their combination determines motoring/regen.
        print(f" [2] Set Target ({'(Signed Hz: +FWD / -REV)' if CONTROL_MODE == 'SPEED' else '(Signed % Torque Command: +FWD / -REV)'})")
        print(" [3] Send RUN Command")
        print(" [4] Send STOP Command")
        print(" [5] Reset Fault")
        print(" [6] Toggle Control Mode in Script (Current: " + CONTROL_MODE + ")")
        print(" [q] Quit")

        choice = input("\nSelect Option > ").strip().lower()

        if choice == "1":
            read_telemetry(vfd)
        elif choice == "2":
            val_str = input(f"Enter Target Value ({'(Signed Hz: +FWD / -REV): ' if CONTROL_MODE == 'SPEED' else '(Signed % Torque Command: +FWD / -REV)'}): ").strip()
            try:
                val = float(val_str)
                set_target_value(vfd, CONTROL_MODE, val)
            except ValueError:
                print("[ERROR] Invalid numeric input.")
        elif choice == "3":
            send_run_command(vfd)
        elif choice == "4":
            send_stop_command(vfd)
        elif choice == "5":
            send_fault_reset(vfd)
        elif choice == "6":
            CONTROL_MODE = "TORQUE" if CONTROL_MODE == "SPEED" else "SPEED"
            print(f"[SCRIPT] Script Target Mode updated to: {CONTROL_MODE}")
            print("Note: Ensure Pr.00-10 on the VFD keypad matches this selection!")
        elif choice == "q":
            print("Exiting test script...")
            break
        else:
            print("Invalid choice. Try again.")


if __name__ == "__main__":
    main()