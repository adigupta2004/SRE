# Regenerative Dynamometer Control Software

Python software for a plug-and-play regenerative dynamometer. A 30 kW induction motor,
driven by a Delta C2000 VFD with a Delta REG2000 regenerative unit, loads a Machine
Under Test (MUT) coupled to its shaft and returns the absorbed energy to the grid. The
PC talks to the C2000 over Modbus RTU (RS-485) to command torque and read telemetry; a
Baumer encoder on the dyno shaft provides signed shaft speed.

The final application (`dyno_app.py`, a GUI with torque-profile, custom-function and
drivecycle modes) is being built step by step: each `test_XX_*.py` script proves one
part of it on the real hardware, and all of them share the code in `hardware/`,
`control/`, `gui/` and `utils/`.

## Setup

1. **Python environment** — create a Python 3.11 virtual environment (it is not
   stored in git) and install the dependencies, including PySide6 and pyqtgraph for
   the GUI:
   ```
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
   Afterwards, just `source .venv/bin/activate` before running a script.
2. **RS-485 link** — connect the USB-RS485 adapter A+ → C2000 SG+, B− → SG−, with the
   C2000's 485 termination switch at "120" and a 120 Ω resistor across A+/B− at the
   adapter end (twisted pair).
3. **Serial port** — set `PORT_NAME` in [config.py](config.py) to your
   adapter (macOS: `ls /dev/tty.usbserial*`; Linux: usually `/dev/ttyUSB0`).
4. **Drive settings** — the C2000 must match the link settings in `config.py`:
   Pr.09-00 = 1 (address), Pr.09-01 = 38.4 kbps, Pr.09-04 = 14 (8,E,1 RTU),
   Pr.09-30 = 0. The full parameter list is in the project overview document.

Scripts live in the repository root, so imports from `hardware/`, `control/`, `gui/`
and `utils/` work without any setup.

## Scripts

| Script | Status | What it does |
|---|---|---|
| `test_01_vfd_core.py` | Done (speed control) | Interactive menu: RUN / STOP / fault reset, speed (Hz) or torque (%) target, telemetry readout. The control mode chosen in the script must match Pr.00-10 on the keypad. |
| `test_02_encoder.py` | Done | **Read-only** shaft-speed monitor: live signed RPM from the encoder, compared with the C2000's own estimate. Mode 1 also checks the encoder scaling while you turn the shaft slowly by hand. Never sends commands to the drive. |
| `test_03_coupled_torque.py` | Done | First coupled torque test (MUT running forward from its keypad). Stages: MUT only → dyno RUN at 0 % → −2 % sign check (must oppose the MUT) → constant and ramped torque. Every command is limited by `MAX_TEST_TORQUE_PCT` at the top of the script; every exit, including Ctrl+C, ramps to 0 %, writes 0 % and sends STOP. Needs Pr.09-03 = 0.0 (comm timeout off). |
| `test_04_torque_runtime.py` | Written, to be run on the rig | GUI test of the reusable ~10 Hz runtime: choose a torque profile (constant, ramp, step, linear vs RPM), Torque is entered, plotted and logged in N·m. Start → 0 N·m + RUN → slew into the profile → run with live torque and speed plots → Stop or profile end → ramp to 0, write 0 N·m, STOP. Refuses to start if Pr.11-27 on the drive differs from `config.PR_11_27_PCT` (the N·m scaling depends on it). Writes one CSV per run to `logs/` ("Save CSV log" checkbox, on by default) and shows commanded torque against two estimates: the C2000's own (scored: error plot and bias / MAE / max error) and electrical output power / shaft speed (plotted only; it includes the dyno motor's losses, so it reads about losses/ω more positive than the shaft torque). Limits are in `config.py` (COMMISSIONING LIMITS). |
| `test_05_…` | Planned | Drivecycle / road-load simulation |
| `dyno_app.py` | Planned | Final GUI |

Example:
```
python test_02_encoder.py
```
Press Ctrl+C to stop a monitoring script; `q` quits the Script 1 menu. In Script 3,
Enter leaves a live display and Ctrl+C is a controlled shutdown. Script 4 opens a
window; closing it (or Ctrl+C in the terminal) stops any active run first.

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
config.py          settings you may need to change (port, encoder, loop rate, limits)
hardware/          everything that talks to or describes a device
  comms.py         opens the Modbus RTU connection
  registers.py     C2000 Modbus register addresses and command words
  c2000.py         C2000 driver: run/stop/reset, speed and torque targets, telemetry
  encoder.py       signed shaft RPM from the encoder position counter
control/           dyno control logic, no Modbus and no GUI code
  torque_profiles.py   torque target vs time and RPM (constant, ramp, step, linear vs RPM)
  runtime.py       the ~10 Hz read -> profile -> limits -> command -> log loop
  runtime_process.py  runs that loop in its own process so the GUI cannot slow it down
  run_log.py       one CSV file per run in logs/
  validation.py    commanded vs estimated torque error: bias, MAE, max error
gui/               reusable PySide6 + pyqtgraph widgets; never talks to the hardware
  trace_plot.py    named traces, legend, show/hide, autoscale, clear
  profile_editor.py    profile picker with fields generated from each profile
  telemetry_panel.py   live label/value grid
  dyno_window.py   main window for running profiles
utils/             generic helpers with no hardware knowledge
  mathutils.py     counter arithmetic, unit conversion, low-pass filter
  timing.py        fixed-rate loop timing
  console.py       terminal display helpers (banners, status line, menus)
test_XX_*.py       step-by-step hardware test scripts
logs/              CSV run logs (kept out of git)
Manuals/           datasheets and project overview (kept out of git)
```

To add a new script: put anything the final application will also need into
`hardware/` (device-specific), `control/` (dyno logic), `gui/` (widgets) or `utils/`
(generic), and keep only the test-specific orchestration in the script. Settings belong in
`config.py` and register numbers in `registers.py`; scripts should not hard-code
either. See [CLAUDE.md](CLAUDE.md) for the full design notes and register details.
