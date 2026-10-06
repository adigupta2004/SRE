# Regenerative Dynamometer Control Software

Python software for a plug-and-play regenerative dynamometer. A 30 kW induction motor,
driven by a Delta C2000 VFD with a Delta REG2000 regenerative unit, loads a Machine
Under Test (MUT) coupled to its shaft and returns the absorbed energy to the grid. The
PC talks to the C2000 over Modbus RTU (RS-485) to command torque and read telemetry; a
Baumer encoder on the dyno shaft provides signed shaft speed.

The final application (`dyno_app.py`, a GUI with torque-profile, custom-function and
drivecycle modes) is being built step by step: each `test_XX_*.py` script proves one
part of it on the real hardware, and all of them share the code in `dyno/`.

## Setup

1. **Python environment** — the repo includes a Python 3.11 virtual environment:
   ```
   source .venv/bin/activate
   ```
   On a new machine, create one instead:
   ```
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
2. **RS-485 link** — connect the USB-RS485 adapter A+ → C2000 SG+, B− → SG−, with the
   C2000's 485 termination switch at "120" and a 120 Ω resistor across A+/B− at the
   adapter end (twisted pair).
3. **Serial port** — set `PORT_NAME` in [dyno/config.py](dyno/config.py) to your
   adapter (macOS: `ls /dev/tty.usbserial*`; Linux: usually `/dev/ttyUSB0`).
4. **Drive settings** — the C2000 must match the link settings in `config.py`:
   Pr.09-00 = 1 (address), Pr.09-01 = 38.4 kbps, Pr.09-04 = 14 (8,E,1 RTU),
   Pr.09-30 = 0. The full parameter list is in the project overview document.

Always run scripts from the repository root, so that `import dyno` works.

## Scripts

| Script | Status | What it does |
|---|---|---|
| `test_01_vfd_core.py` | Done (speed control) | Interactive menu: RUN / STOP / fault reset, speed (Hz) or torque (%) target, telemetry readout. The control mode chosen in the script must match Pr.00-10 on the keypad. |
| `test_02_encoder.py` | Done | **Read-only** shaft-speed monitor: live signed RPM from the encoder, compared with the C2000's own estimate. Mode 1 also checks the encoder scaling while you turn the shaft slowly by hand. Never sends commands to the drive. |
| `test_03_coupled_torque.py` | Next | Coupled torque control with the MUT, including regeneration through the REG2000 |
| `test_04` … `test_07` | Planned | Control loop + logging, torque validation, torque profiles, drivecycle simulation |
| `dyno_app.py` | Planned | Final GUI |

Example:
```
python test_02_encoder.py
```
Press Ctrl+C to stop a monitoring script; `q` quits the Script 1 menu.

## Safety

- The MUT is started and stopped separately, from the CastNX VFD keypad. See the MUT
  section of the project overview for its startup and shutdown order relative to the dyno.
- Normal stop: bring the torque command to 0 %, confirm it, *then* send STOP. The
  C2000 is set to coast to a stop.
- Before RUN or a fault reset, the software must be commanding STOP and 0 % torque.
- Fit the blue induction-motor fan for any coupled test above 5 kW.
- Do not approach the machine until the shaft has fully stopped.

## Code layout

```
dyno/
  config.py      settings you may need to change (port, encoder, loop rate, limits)
  registers.py   C2000 Modbus register addresses and command words
  comms.py       opens the Modbus RTU connection
  c2000.py       C2000 driver: run/stop/reset, speed and torque targets, telemetry
  encoder.py     signed shaft RPM from the encoder position counter
  mathutils.py   counter arithmetic, unit conversion, low-pass filter
  timing.py      fixed-rate loop timing
  console.py     terminal display helpers
test_XX_*.py     step-by-step hardware test scripts
Manuals/         datasheets and project overview (kept out of git)
```

To add a new script: put anything the final application will also need into `dyno/`
and keep only the test-specific menus and checks in the script. Settings belong in
`config.py` and register numbers in `registers.py`; scripts should not hard-code
either. See [CLAUDE.md](CLAUDE.md) for the full design notes and register details.
