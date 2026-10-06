# Regenerative Dynamometer (SRE) — project context

Python software to turn an existing induction-motor test rig into a **plug-and-play
regenerative dynamometer**. The user couples a Machine Under Test (MUT) to the dyno
motor and controls everything from a PC GUI. Development proceeds through numbered
test scripts, each proving one slice of the final app; they all share the code in
`hardware/` and `utils/` so nothing is rewritten for the final `dyno_app.py`.

The authoritative spec is `Manuals/Project overview.pdf` (tabs: Goals, Plan, Dyno side
docs, MUT side docs). Read it before any non-trivial change.

## Hardware

```
AC mains ─ EMC filter ─ Delta C2000 VFD ─ 30 kW loading IM (dyno) ══ mech coupling ══ 11 kW MUT IM ─ CastNX VFD ─ AC mains
                            │ DC+/DC−                 │ encoder (Baumer, 1024 PPR)
                       DC choke ─ Delta REG2000 ─ AC mains (regen back to grid)
PC (Python) ─ USB↔RS-485 ─ C2000 (Modbus RTU, slave 1, 38400 8E1)
```

- **Dyno side** — Delta C2000 drives the loading IM; normal operation is **torque
  control** (Pr.00-10 = 2, currently IM TQC *Sensorless*; IM TQCPG with encoder is the
  next step). Python only sends RUN/STOP + a torque target (Pr.11-34); the C2000 runs
  the motor control loop. Speed mode (Pr.00-10 = 0) is for commissioning only.
- **Encoder** — Baumer EB260F (1024 PPR, TTL/RS422 at 5 V) + EBS.R-2R052 magnetic ring
  (≤1 mm air gap) → Delta EMC-PG01L card in C2000 slot 2. 4096 quadrature counts/rev.
- **REG2000** — independent of Python; in Automatic Mode (MI1–DCM) it returns DC-bus
  energy to mains above an activation level. It also has its own Modbus RS-485 port
  (not used yet; could share the bus at another slave address for monitoring).
- **MUT** — CastNX 11 kW, 2-pole, 415 V, 2940 rpm, 19 A, ~35.7 N·m rated; CastNX
  EmotionX EMOX-11K VFD. Operated **manually from its keypad** at constant speed;
  probably cannot do torque control. Not controlled by this software (it has Modbus,
  unused). Older doc sections that say "Shakti VFD" refer to this MUT drive.
- The 30 kW dyno motor's nameplate is not in the repo (vendor set Pr.01-01, 01-02,
  05-01..05-09). Rated torque [N·m] = Pr.05-02 [kW]·1000 / (2π·Pr.05-03 [rpm]/60) — read the params from the
  drive (Modbus address 0x05nn) if Nm conversions are needed.

## Code layout

```
config.py             user-editable settings: serial port/link, encoder PPR, loop rate, filter, limits
hardware/             device-specific code (talks to or describes a device)
  comms.py            open_instrument(): the only place pyserial/minimalmodbus is configured
  registers.py        C2000 Modbus register map + command words (fixed hardware facts)
  c2000.py            C2000 driver class: run/stop/reset_fault/set_frequency/set_torque,
                      read_telemetry() -> Telemetry, read_encoder_sample() -> EncoderSample
  encoder.py          ShaftSpeedEstimator: EncoderSample stream -> signed RPM (+ filtered)
utils/                generic helpers, no hardware knowledge
  mathutils.py        pure math: combine_words, to_signed32, wrap_delta, counts_to_rpm, LowPassFilter
  timing.py           RatePacer (fixed-rate loop, no catch-up bursts)
  console.py          banner, rule, live status line (show_status/log), menu
test_01_vfd_core.py   Script 1: interactive C2000 control + telemetry menu
test_02_encoder.py    Script 2: READ-ONLY encoder shaft-speed monitor (+ scale cross-check)
Manuals/              datasheets + project overview (PDFs, not tracked in git)
```

Layering rules (keep these when adding scripts):
- Scripts own only UI and test-specific logic; anything a later script or the final
  app will need goes into `hardware/` (device-specific) or `utils/` (generic). New
  areas (control logic, data logging, GUI) get their own top-level folder only when
  they are actually written — keep the structure generic and simple.
- Register addresses/encodings only in `registers.py` and `c2000.py`. Tunables only in
  `config.py`. Never hard-code a port, baud rate or register number in a script.
- Library code **raises** on comm failure and **never prints**; scripts catch, log and
  decide (continue / retry / stop). Validation errors raise `ValueError`.
- `mathutils.py` stays pure (no I/O) so it can be tested without hardware.
- Test scripts and `dyno_app.py` stay in the repo root so `import config`,
  `from hardware...` and `from utils...` work with no path setup (decided deliberately;
  revisit only if the root gets crowded).

## Running

```
source .venv/bin/activate        # Python 3.11, minimalmodbus 2.1.1, pyserial 3.5
python test_01_vfd_core.py
```
`config.PORT_NAME` is the macOS USB-RS485 adapter path; change it on another machine.
No hardware is available to the assistant: verify changes by compiling and by driving
scripts with a fake `minimalmodbus.Instrument` (patch the class, script reads, record
writes), and ask the user to run on the rig.

## Git

Everything is tracked (including `.venv/`) except `__pycache__/`, `*.pyc`, `.DS_Store`
and `Manuals/` (large PDFs, intentionally kept out of git). User-facing usage docs are
in `README.md`; keep its script table in step with the plan.

## Plan status (from the overview's Plan tab)

1. Script 1 speed control — DONE. (Torque-command part to be finished later; not blocking.)
2. MUT setup, manual constant-speed operation — DONE.
3. Mechanical coupling — IN PROGRESS.
4. Encoder hardware + C2000 PG interface — DONE (4096 counts/rev verified).
5. Script 2 encoder & shaft speed — DONE.
6. **Next: configure C2000 for IM TQCPG, then Script 3** `test_03_coupled_torque.py`:
   coupled constant/ramped torque, torque sign & quadrants, monitor torque/current/
   power/DC bus, regen through REG2000, torque-mode speed limits, normal stop
   (torque → 0, then STOP). Fit the blue IM fan for any coupled test above 5 kW.
7. Then in order: Script 4 `test_04_control_loop.py` (10 Hz loop, limits, CSV logging,
   comm-error handling, ramp-to-zero before STOP, loop timing), Script 5
   `test_05_torque_validation.py` (estimated actual torque, T = P/ω cross-check),
   Script 6 `test_06_profiles.py` (Modes A/B: constant, T=mω+c, T=k/ω, step,
   polynomial in ω or t), Script 7 `test_07_drivecycle.py` (Mode C road-load).
8. Final GUI `dyno_app.py` only after 1–7 are proven; it must only integrate proven code.

## Operating rules the software must respect

- **Normal stop:** ramp Pr.11-34 to 0 %, confirm, then STOP (C2000 is coast-stop, Pr.00-22 = 1).
- Before RUN or fault reset: Python must be commanding STOP and 0 % torque.
- Comm loss: Pr.09-02 = 2 (fault + coast) with Pr.09-03 timeout — intended 1.0 s, but
  **currently 0.0 s (disabled) for testing**. A loop must keep talking to the drive
  at ~10 Hz once the timeout is enabled.
- Torque-mode speed limits are Pr.11-37/11-38 (% of Pr.01-00 = 100 Hz); they are speed
  regulation, not a fault — a separate software/hardware overspeed trip is still TODO.
- Software caps on max torque, rpm and power are required in all modes (GUI warns when hit).
- E-stop is hardwired STO (future), independent of Python.
- Script 2 is strictly read-only; keep any commissioning/diagnostic script that way
  unless it is explicitly a control test.

## Hard-won register facts

- Pr.GG-nn Modbus address = `0xGGnn` (`registers.param_address`). Pr.11-34 = 0x0B22,
  signed, 0.1 % units, range −100..+100 % of Pr.11-27.
- 2000H: STOP = 0x0001, RUN = 0x0002, RUN FWD = 0x0012, RUN REV = 0x0022. 2001H freq
  command (0.01 Hz, magnitude only — direction comes from 2000H). 2002H bit1 = reset.
- 2207H motor speed is **unsigned** and unreliable (drops to 0 on STOP) — comparison only.
- 2200H output current: decimal places are in the **high byte of 211FH**.
- 2208H output torque %: signed. 2101H bits 1-0: Stopped/Decelerating/Standby/Operating.
- Shaft speed: 222CH (low) / 222DH (high) = free-running 32-bit position counter,
  4096 counts/rev; speed = signed modulo-2^32 delta / dt. 2209H = PG position
  0..4095 per rev (wraps every rev; only for slow-speed cross-checks).
  FWD (A leads B, Pr.10-02 = 1) = positive RPM.
- Likely needed for Script 3 (from the manual, **not yet in `registers.py` or verified
  on hardware**): 2100H (warning/error code), 2223H (control mode), 2227H (torque N·m),
  2228H (torque command readback). Add them to `registers.py` only when used.
- Telemetry is currently one transaction per register (~8 per read). For Script 4's
  10 Hz budget, consider contiguous block reads (e.g. 2200H–2209H) — verify on hardware first.
- Modbus exception codes: 1 bad function, 2 bad address, 3 bad data, 4 execution failure.

## Reading the manuals

PDFs are large (C2000 manual: 1173 pages). Use `pdftotext -layout` into a scratch dir
and grep. Useful anchors in the C2000 text: "4. Address list" (Modbus map, Ch. 12
Group 09, page 12.1-09-8), "11-34"/"11-36" (torque params), "Exception response".
REG2000 Modbus map: grep "2203H" in its text. The Baumer detailed manual is mostly
IO-Link parameterisation (not used; encoder runs as plain incremental ABZ).
