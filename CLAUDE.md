# Regenerative Dynamometer (SRE) — project context for Claude

Python software that turns an induction-motor test rig into a plug-and-play
regenerative dynamometer: a Machine Under Test (MUT) is coupled to the dyno motor and
everything is controlled from a PC GUI. The dyno software is `main.py` (formerly Script 4,
`test_04_torque_runtime.py`); the numbered `test_01..03` scripts that proved the hardware
step by step remain as commissioning/debugging tools. All of them share the code in
`hardware/`, `control/`, `gui/` and `utils/`.

The authoritative spec is `Manuals/Project overview.pdf` (tabs: Goals, Plan, Dyno side
docs, MUT side docs). Read it before any non-trivial change. User-facing docs are in
`README.md` (keep its program table in step with the code).

## Hardware

```
AC mains ─ EMC filter ─ Delta C2000 VFD ─ 30 kW loading IM (dyno) ══ coupling ══ 11 kW MUT IM ─ CastNX VFD ─ AC mains
                            │ DC+/DC−                 │ encoder (Baumer, 1024 PPR)
                       DC choke ─ Delta REG2000 ─ AC mains (regen back to grid)
PC (Python) ─ USB↔RS-485 ─ C2000 (Modbus RTU, slave 1, 38400 8E1)
```

- **Dyno (C2000):** torque control in **IM TQCPG** with encoder feedback (Pr.00-10 = 2,
  Pr.00-13 = 0, Pr.11-33 = 1). Python sends only RUN/STOP and a torque target (Pr.11-34);
  the drive runs the motor control loop. Speed mode (Pr.00-10 = 0) is for commissioning only.
- **Encoder:** Baumer EB260F (1024 PPR, TTL/RS422 5 V) + EBS.R-2R052 magnetic ring
  (≤ 1 mm air gap) → EMC-PG01L card in C2000 slot 2; 4096 quadrature counts/rev.
- **REG2000:** independent of Python (Automatic Mode, MI1–DCM); returns DC-bus energy to
  mains. Has its own Modbus port, unused.
- **MUT:** CastNX 11 kW, 2-pole, 415 V, 2925 rpm, 19 A, ~35.9 N·m; CastNX EmotionX
  EMOX-11K VFD, run manually from its keypad at constant speed, not controlled by this
  software. Older docs that say "Shakti VFD" mean this drive.
- **Dyno motor:** 30 kW, 2900 rpm, 2-pole (nameplate not in the repo; the vendor set
  Pr.05-01..05-09). `config.py` holds the torque scaling: inputs MOTOR_RATED_POWER_KW,
  MOTOR_RATED_RPM, PR_11_27_PCT (36); derived MOTOR_RATED_TORQUE_NM (98.79, 100 % of 2208H)
  and TORQUE_REFERENCE_NM (35.56, 100 % of Pr.11-34).

## Code layout

Each module's docstring explains it in detail; this is the map.

```
main.py                the dyno software: starts RuntimeProcess, opens DynoWindow (viewer-only if no drive)
config.py              user-editable settings: link, encoder, loop rate, torque scaling, limits, logs,
                       no-load loss table
hardware/              device-specific code
  comms.py             open_instrument(): the only place pyserial/minimalmodbus is configured
  registers.py         C2000 register map + command words (fixed hardware facts)
  c2000.py             C2000 driver: commands, N·m conversion, scaling check, block reads
  encoder.py           ShaftSpeedEstimator: encoder samples -> signed RPM (+ filtered)
control/               dyno logic: no Modbus details, no GUI code
  torque_profiles.py   TorqueProfile subclasses (N·m vs time/rpm), PARAMS drive the GUI fields
  runtime.py           DynoRuntime: the ~10 Hz loop, single-threaded run(next_command)
  runtime_process.py   RuntimeProcess: runs DynoRuntime in its own process; the GUI's handle
  run_log.py           CSV run logs: RunLog writes, read_log() reads columns back as numpy
  validation.py        torque error (estimated torque vs command in force) and its statistics
gui/                   PySide6 + pyqtgraph; never talks to the drive
  trace_plot.py        TracePlot: named traces, checkbox legend, live window or whole arrays,
                       optional hover cursor (values of all traces; used by LogView)
  quantities.py        QUANTITIES: what the live and results plots can show (shared list)
  log_view.py          LogView: the Results tab / run-log viewer (works without a drive)
  dyno_window.py       DynoWindow: running the dyno; hosts LogView; viewer-only if no runtime
  profile_editor.py, telemetry_panel.py   small widgets
utils/                 generic helpers: mathutils (pure), timing (RatePacer), console
test_01..03_*.py       Scripts 1-3: commissioning/debugging tools (see Plan status)
logs/, Manuals/        run logs and PDFs, not tracked in git
```

## Architecture rules

- `main.py` and the scripts hold only orchestration (and test-specific logic); anything
  shared goes into `hardware/`, `control/`, `gui/` or `utils/`. `main.py` and the scripts
  stay in the repo root so imports need no path setup.
- **With a GUI, the loop runs in its own process** (`RuntimeProcess`), never as a thread of
  the GUI process: threads share the GIL, and GUI work then stalls the loop and skews
  encoder timestamps (seen on the rig). Only the control process opens the serial port;
  only `DynoRuntime.run()` touches the C2000. The GUI calls only `start_run(profile,
  log_location)`, `request_stop()`, `poll()` and `close()`.
- Torque is **N·m everywhere above the drive**; % exists only in `c2000.py`. Profiles
  return raw targets; the runtime clamps to the config limits and owns slewing
  (`TORQUE_SLEW_NM_PER_S`). Scripts 1 and 3 still work in %.
- Register addresses/encodings only in `registers.py` and `c2000.py`; tunables only in
  `config.py`; never hard-code a port, baud rate or register number in a script.
- Library code raises on comm failure and never prints; scripts catch, log and decide.
  Validation errors raise `ValueError`. `mathutils.py` stays pure (no I/O).
- Nothing per-sample is kept in memory during a run (runs may last hours): results are
  read back from the CSV. Live plots show a bounded rolling window.
- Every Modbus transaction costs ~20 ms on this drive (RTU frames need ≥ 10 ms silence):
  keep transactions per cycle minimal (block reads; currently 3 reads + 1 write).

## Product decisions (from the user)

- Validation statistics: bias, MAE and max |error| only, scoring `estimated_torque_nm`
  only (Oct 2026; previously the C2000 estimate).
- The GUI stays clean: no caveats, footnotes or explanatory notes in the window. The CSV
  header is two `#` lines (start time, profile). Caveats live in code comments.
- Power is `power_vi_kw` = √3·V·I·cos(angle) from 2204H/2200H/2205H; 2206H is not read
  (finer, no truncation; reasons in `C2000.Telemetry.power_vi_kw`).
- Torque quantities: `estimated_torque_nm` = (power_vi − no-load losses(rpm)) / ω, the scored
  estimate: the dyno's electromagnetic torque (the coupling torque adds the dyno's own
  friction/windage, not measured; loss table `config.NO_LOAD_LOSS_*`, measured with 0 N·m
  runs, electrical losses only); `torque_elec_nm` =
  power_vi / ω (after the motor's losses), logged and plotted, not scored; `torque_c2000_nm`
  (2208H) kept but documented as not usable yet.
- Logs go to `<log location>/logs/`, location chosen in the GUI (default: project root,
  not remembered). The window opens as a log viewer when the drive isn't connected.

## Testing without hardware

No hardware is available to the assistant. Verify by compiling and by driving code with
a fake `minimalmodbus.Instrument` (raw 16-bit register map; record writes); ask the user
to run on the rig. For anything using `RuntimeProcess`:
- pass `RuntimeProcess(vfd_factory=...)` an importable module-level function returning
  `C2000(fake_instrument)`; patches and `config` changes in the parent do not reach the
  spawned control process (pass `start_run(..., log_location=<temp dir> or None)`);
- keep test scripts' top-level code under `if __name__ == "__main__":`;
- GUI: `QT_QPA_PLATFORM=offscreen`, drive with `QTimer.singleShot`, check
  `window.grab().save(...)`.

## Running and git

```
python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
python main.py
```
`config.PORT_NAME` is the macOS adapter path; change it on another machine. Git tracks
everything except `__pycache__/`, `*.pyc`, `.DS_Store`, `.venv/`, `logs/`, `Manuals/`.
Keep `requirements.txt` updated whenever a package is installed.

## Plan status

1. Script 1 (`test_01_vfd_core.py`) speed control — DONE (torque part not blocking).
2. MUT setup, manual constant-speed operation — DONE.
3. Mechanical coupling — DONE. The 30 kW dyno motor's fan must be on (connector plugged
   into the supply) for any coupled test above 5 kW.
4. Encoder + C2000 PG interface — DONE (4096 counts/rev verified).
5. Script 2 (`test_02_encoder.py`) encoder & shaft speed — DONE. Strictly read-only.
6. C2000 in IM TQCPG. Script 3 (`test_03_coupled_torque.py`) — DONE on the rig;
   deliberately atomic (no logging/plots/fault handling), works in %.
7. The dyno software, `main.py` (formerly Script 4, `test_04_torque_runtime.py`) — DONE,
   runs on the rig; it is the final software (Oct 2026): profiles, live plots, CSV, results
   viewer with hover cursor, validation. Loop holds 10 Hz (cycle_time ~45–49 ms, flat over
   60 s+ runs). Scores `estimated_torque_nm`; open: the torque findings below.

## Open issues

- **C2000 torque estimate (2208H) is not usable** (rig tests 25–28 Oct 2026, TQCPG). It
  reads 0.83 × command at 5–30 N·m and 490–2500 rpm; our scaling is confirmed (2227H
  agrees, 2228H echoes the command unchanged; 210BH is the same value too). It is a
  motor-model value: it follows the command almost instantly, even while the flux builds
  up after RUN, and stays flat through the real transients below. The delivered torque is
  close to the command: `estimated_torque_nm` = 30.3 N·m at −30 N·m / 2470 rpm (power
  ~7.3 kW, measurement error ~1 %), 16.0 N·m settled at −15 N·m / 2490 rpm (within the
  ~3 % measurement uncertainty at 3.6 kW). So the estimate is ~17–22 % below the real
  torque. **Do not rescale in software.** Suspected: motor-1 parameters (Pr.05-01,
  05-05..05-09; auto-tune Pr.05-00) — the user does not want them changed for now;
  changing them may also change the delivered torque. Independent checks still open:
  torque arm at standstill, inline torque transducer.
- **Delivered-torque transients** (seen in power_vi, not in 2208H): after RUN the drive
  builds the motor's flux in ~4 s (2204H voltage ~4 V → full), so torque commanded during
  ENTERING arrives late. At −15 N·m / 2500 rpm the torque then settles over ~40 s
  (τ ≈ 10 s; +12 % at first after RUN, −3 % after a step down from −30 N·m, settled in
  ~15–25 s); at −30 N·m there is no such drift. Current stays constant meanwhile, only its
  split between flux and torque changes: a slow internal state of the drive's control,
  not motor temperature. Handling (wait before scoring, etc.) is still to be decided.
- `estimated_torque_nm` relies on the drive's own voltage value (2204H is probably the
  commanded voltage) and on the no-load loss table (10 speeds, ~500–2775 rpm, measured
  29 Oct 2026; excludes the extra copper loss under load). At ~1–2 kW the power figures
  may be off by ~50–100 W (0.5–1 N·m at 1000 rpm), so low-power results are less certain.
- The no-load losses rise with speed up to ~2265–2520 rpm and then fall (446 W at 2774 rpm,
  steady values), unlike iron loss. Not flux reduction: current (10.12 A) and V/f
  (8.0–8.3 V/Hz) are constant over the sweep; only the power angle moves towards 90°
  (84.6° → 86.1°). Either the drive's torque is not exactly 0 at high speed (~−0.6 N·m
  would explain it) or 2205H is slightly off there (cos φ ≈ 0.07–0.09, so 1° ≈ 15 % of
  power). Unresolved. The table in config.py (entered 29 Oct) averages whole runs,
  including the flux build-up after RUN, so it is 5–9 % below the steady values.
- Comm-loss timeout Pr.09-03 is 0.0 s (disabled) for testing; intended 1.0 s. The loop
  keeps talking at 10 Hz in its own process, so enabling it is a rig decision.
- A software/hardware overspeed trip is still TODO (Pr.11-37/11-38 only regulate speed).
- E-stop: hardwired STO (future), independent of Python.

## Operating rules the software must respect

- Normal stop: ramp the torque to 0, write 0, then STOP (C2000 is coast-stop, Pr.00-22 = 1).
- Before RUN or fault reset: Python must be commanding STOP and 0 torque.
- Software caps on torque, rpm and power apply in all modes.
- The runtime refuses to start unless Pr.05-02, Pr.05-03 and Pr.11-27 match `config.py`
  (`C2000.verify_torque_scaling()`).

## Register facts (verified on the rig unless stated)

- Pr.GG-nn Modbus address = `0xGGnn`. Pr.11-34 = 0x0B22: signed, 0.1 %, −100..+100 % of
  Pr.11-27. Pr.11-27 = 0x0B1B (integer % of rated); Pr.05-02 = 0x0502 (0.01 kW);
  Pr.05-03 = 0x0503 (rpm). Drive's rated torque = Pr.05-02 / (Pr.05-03·2π/60).
- 2000H: STOP 0x0001, RUN 0x0002 (torque mode: no direction bits), RUN FWD 0x0012,
  RUN REV 0x0022. 2001H freq command (0.01 Hz, magnitude only). 2002H bit1 = fault reset.
- 2101H bits 1-0: Stopped/Decelerating/Standby/Operating. 2102H/2103H freq command/output.
- 2200H output current (AC, drive → motor): decimal places in the **high byte of 211FH**
  (read once).
- 2204H output voltage, 0.1 V (AC line-line; probably the drive's commanded voltage).
- 2205H power angle: **signed**, 0.1°. |angle| = phase angle between output voltage and
  output current (0–90°); sign + motoring, − regenerating (e.g. +83° at 0 N·m, −40° at
  −30 N·m). Power = sign·√3·V·I·cos(angle), agrees with 2206H.
- 2206H output power: **signed**, 0.1 kW, negative when regenerating; appears to truncate
  (20–80 W nearer zero than 2204H/2205H power). Not read by the code.
- 210BH = 2208H (signed 0.1 %); 210FH = 2206H in 0.1 kW (manual says X.XXX kW — wrong for
  this drive, possibly model-size dependent). Neither is read by the code.
- 2207H motor speed: **unsigned**, drops to 0 on STOP — comparison only.
- 2208H output torque: signed, 0.1 % of **motor rated torque** (not of Pr.11-27 like the
  command). Compare command and estimate only after converting both to N·m.
- 2227H: the same torque estimate in 0.1 N·m (signed). 2228H: the Pr.11-34 command echoed
  back (0.1 %), not the drive's internal reference. Neither is read by the code.
- 222CH (low) / 222DH (high): free-running 32-bit position, 4096 counts/rev; speed =
  signed modulo-2^32 delta / dt; FWD (A leads B, Pr.10-02 = 1) = positive. 2209H = PG
  position 0..4095 per rev (slow-speed cross-checks only).
- Block reads 2101H–2103H, 2200H–2209H (10 words) and 222CH–222DH work; the manual's
  per-read limit ("n ≤ 16" data) is ambiguous. Signed words via `mathutils.to_signed16`.
- Unverified (manual only, not in `registers.py`): 2100H warning/error code, 2223H
  control mode. Add to `registers.py` only when used.
- Modbus exception codes: 1 bad function, 2 bad address, 3 bad data, 4 execution failure.

## Reading the manuals

PDFs are large (C2000 manual: 1173 pages): `pdftotext -layout` into a scratch dir and
grep. C2000 anchors: "4. Address list" (Modbus map, Ch. 12 Group 09, p. 12.1-09-8),
"11-34"/"11-27" (torque params), "05 Motor Parameters", "Exception response". REG2000
Modbus map: grep "2203H". The Baumer detailed manual is mostly IO-Link (not used).
