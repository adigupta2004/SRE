"""
Dyno runtime: the reusable ~10 Hz control loop.

This module is the loop itself and knows nothing about processes or GUIs.
control/runtime_process.py runs it in a separate process, so that the GUI cannot
slow it down (see there for why); scripts without a GUI can use DynoRuntime directly.

run() is the loop; it owns the C2000 while it runs. Every cycle it:
    reads the encoder sample and C2000 telemetry -> signed shaft RPM
    evaluates the active torque profile (actual elapsed time, actual RPM)
    clamps to the commissioning torque limits -> writes the torque setpoint
    measures loop timing -> logs the sample -> publishes it
and paces itself with RatePacer. Nothing here waits on a GUI: results are
published through callbacks, which must return quickly (e.g. put the event on a queue).

Phases:
    IDLE      drive STOPPED, read-only monitoring (no torque writes, no logging)
    ENTERING  RUN sent at 0 N·m; torque setpoint slews to the profile's starting target
    RUNNING   profile active; its clock starts when ENTERING completes
    STOPPING  torque setpoint slews to 0, then 0 N·m is written explicitly and STOP sent

Controlled shutdown (-> STOPPING) on: Stop request, profile end, |RPM| above
MAX_ABS_RPM, RPM below MIN_RUN_RPM, implausible encoder data, repeated comm
errors, or an unexpected error in the loop.
"""

import time
from dataclasses import dataclass

import config
from control.run_log import RunLog
from hardware.encoder import ShaftSpeedEstimator
from utils.mathutils import interp_clamped, power_to_torque_nm
from utils.timing import RatePacer

IDLE = "IDLE"
ENTERING = "ENTERING"
RUNNING = "RUNNING"
STOPPING = "STOPPING"


@dataclass(frozen=True)
class Sample:
    phase: str
    elapsed_time: float           # s since RUN (None in IDLE)
    loop_dt: float                # s since the previous cycle started
    cycle_time: float             # s spent on this cycle's reads/writes
    overruns: int                 # cycles so far this run with cycle_time > loop period (None in IDLE)
    profile_time: float           # s since the profile started (None outside RUNNING)
    profile_target_nm: float      # raw profile target before limits (None outside ENTERING/RUNNING)
    torque_command_nm: float      # torque in force after this cycle's write: last one the drive accepted (None in IDLE)
    limited: bool                 # target was clamped to the torque limits
    encoder_rpm: float            # raw signed encoder RPM (None if implausible)
    filtered_rpm: float
    c2000_rpm: int                # 2207H, unsigned
    torque_c2000_nm: float        # 2208H in N·m: the drive's motor-model torque, signed. Not a usable
                                  # torque reading yet (~17 % low, does not show real transients):
                                  # see C2000.Telemetry.torque_nm. Logged and plotted, not scored.
    estimated_torque_nm: float    # estimated shaft torque: (power_vi - no-load losses(rpm)) / ω.
                                  # The best torque figure available; the validation statistics score it.
    torque_elec_nm: float         # electrical torque: power_vi / ω, i.e. after the dyno motor's
                                  # losses (in regen it is less negative than the shaft torque by
                                  # about losses/ω). Logged and plotted, not scored.
                                  # Both None below MIN_RUN_RPM or with implausible encoder data.
    output_current_a: float       # output current (AC, drive -> motor)
    output_voltage_v: float       # output voltage (AC line-line)
    power_angle_deg: float        # signed angle between output voltage and current (+ motoring, - regen)
    power_vi_kw: float            # output power sqrt(3)·V·I·cos(angle), signed (negative = regenerating)
    dc_bus_v: float
    drive_state: str              # 2101H state, e.g. "Operating"


@dataclass(frozen=True)
class RunResult:
    """
    Summary of a finished run. It holds no per-sample data, so it stays small for runs
    of any length: the samples themselves are in the CSV (log_path), which is where
    plots and validation statistics are computed from.
    """
    profile: str                  # profile description
    reason: str                   # why the run stopped
    log_path: str                 # None if the run was not logged to CSV
    duration_s: float
    cycles: int                   # samples taken during the run (ENTERING .. STOPPING)
    mean_loop_dt: float
    overruns: int                 # cycles whose work took longer than the loop period


def slew(value, target, max_step):
    """
    One step of a rate-limited ramp: moves value toward target by at most max_step.

    Called once per cycle with max_step = TORQUE_SLEW_NM_PER_S * the cycle's actual dt,
    so the ramp keeps its rate in N·m/s even if the loop runs slower than nominal. Used
    only for entering a profile (0 -> its start) and stopping (-> 0).

    Once target is within one step it returns target itself, not value +/- max_step,
    so the ramp lands exactly on it (no overshoot, no float residue) and callers can
    test arrival with ==.

    Example, max_step 0.5: 0.0 -> -0.5 -> -1.0 -> -1.5 -> -2.0 (target).
    """
    if abs(target - value) <= max_step:
        return target
    return value + max_step if target > value else value - max_step


def shutdown_timeout_s():
    """
    Worst-case time for a normal shutdown: ramping the largest allowed torque setpoint to
    0 N·m at TORQUE_SLEW_NM_PER_S, plus a margin for the final 0 N·m + STOP and comm
    retries. DynoRuntime.run() gives up a shutdown after this long; RuntimeProcess.close()
    waits this long plus the time for the control process to exit.
    """
    max_torque_nm = max(abs(config.TORQUE_LIMIT_MIN_NM), abs(config.TORQUE_LIMIT_MAX_NM))
    return max_torque_nm / config.TORQUE_SLEW_NM_PER_S + 10.0


class DynoRuntime:
    """
    Single-threaded: run() is the loop and runs in the caller's thread, which then owns
    the C2000. Commands come in only through the next_command function given to run(),
    which is asked for them at the start of every cycle, so nothing else ever touches
    the runtime's state and no locks are needed.

    Commands are (kind, arg) tuples:
        ("start", (profile, log_location))  start a run; its CSV goes into <log_location>/logs
                                            (log_location None: no CSV, results still kept)
        ("stop", reason)                 controlled stop of the active run
        ("exit", None)                   stop any active run, then return from run()
    """

    def __init__(self, vfd, on_sample=None, on_message=None, on_run_finished=None):
        self.vfd = vfd
        # Callbacks: the runtime's only output, called from the loop.
        #   on_sample(Sample)          every successful cycle, IDLE included (~10 Hz)
        #   on_message(str)            events: start/stop, refusals, warnings, errors
        #   on_run_finished(RunResult) once per run, after 0 N·m + STOP
        # Any not given becomes a no-op, so the code can always call them unchecked.
        # Each must return quickly: its time adds to the cycle (runtime_process.py
        # sets them to queue puts, which return at once).
        self.on_sample = on_sample or (lambda sample: None)
        self.on_message = on_message or (lambda text: None)
        self.on_run_finished = on_run_finished or (lambda result: None)

        self.phase = IDLE
        self._period = 1.0 / config.LOOP_RATE_HZ
        self._speed = None            # ShaftSpeedEstimator, seeded by the first sample
        self._rpm = None              # last plausible raw encoder RPM
        self._comm_errors = 0
        self._last_cycle_t = None
        self._exiting = False
        self._exit_deadline = None    # monotonic time by which an exit must have finished

        # Per-run state. Two torque values are kept on purpose ("command" in this module
        # means only the start / stop / exit commands that run() receives):
        #   _torque_setpoint_nm  what we want the drive to have: the unrounded value the
        #                        slew/profile logic works on (rounding it every cycle
        #                        could stall a slow slew).
        #   _torque_in_force_nm  what the drive actually has: the last value whose
        #                        write succeeded. Logs, plots and the torque-error
        #                        statistics use this, so a failed write never shows up
        #                        as a torque the drive did not receive.
        self._profile = None
        self._torque_setpoint_nm = 0.0    # unrounded torque setpoint (N·m)
        self._torque_in_force_nm = 0.0    # last torque setpoint the drive accepted (N·m, after register rounding)
        self._run_t0 = None
        self._profile_t0 = None
        self._stop_reason = None
        self._log = None
        self._log_ok = True           # False once a CSV write failed: the rest of the run is unlogged
        # Run statistics are kept as running totals, never as per-sample lists, so memory
        # stays constant however long a run lasts.
        self._cycles = 0
        self._loop_dt_sum = 0.0       # over cycles after the first (see _cycle)
        self._overruns = 0
        self._elapsed = 0.0           # elapsed_time of the latest sample

    # --------------------------------------------------------------------------
    # Public API: start(), then run()
    # --------------------------------------------------------------------------
    def start(self):
        """
        Commands 0 N·m and STOP and checks that the drive's torque scaling matches
        config (Pr.05-02, Pr.05-03, Pr.11-27). Raises on comm failure or a scaling
        mismatch (ValueError); the drive is then left at 0 + STOP. Call before run().
        """
        self.vfd.set_torque_nm(0.0)
        self.vfd.stop()
        self.vfd.verify_torque_scaling()

    def run(self, next_command):
        """
        The loop, in the caller's thread. next_command() must return the next waiting
        command or None, without blocking. Returns after an "exit" command once any
        active run has gone through the normal shutdown (ramp -> 0 N·m -> STOP): True,
        or False if that took longer than shutdown_timeout_s() (e.g. the drive stopped
        answering).
        """
        # Each iteration: wait for the next slot -> apply waiting commands -> one cycle.
        pacer = RatePacer(config.LOOP_RATE_HZ)
        self._last_cycle_t = time.monotonic()
        while True:
            pacer.wait()
            try:
                self._apply_commands(next_command)
                # An exit first moves an active run to STOPPING; run() only returns once
                # that has finished (back in IDLE), so exiting always goes through
                # ramp -> 0 N·m -> STOP - unless the deadline passes first.
                if self._exiting:
                    if self.phase == IDLE:
                        return True
                    if time.monotonic() > self._exit_deadline:
                        self.on_message("[ERROR] Shutdown did not finish in time - "
                                        "check the drive and use keypad STOP.")
                        return False
                self._cycle()
            except Exception as e:
                # Never let the loop die with the drive running.
                self.on_message(f"[ERROR] Unexpected runtime error: {e!r}")
                self._begin_stop("Unexpected runtime error")

    # --------------------------------------------------------------------------
    # Loop internals
    # --------------------------------------------------------------------------
    def _apply_commands(self, next_command):
        """Applies every command waiting now (an exit command sets self._exiting)."""
        while (command := next_command()) is not None:
            kind, arg = command
            if kind == "start":
                self._start_run(*arg)
            elif kind == "stop":
                self._begin_stop(arg)
            elif kind == "exit":
                if not self._exiting:
                    self._exiting = True
                    self._exit_deadline = time.monotonic() + shutdown_timeout_s()
                self._begin_stop("Runtime closing")

    def _start_run(self, profile, log_location):
        # Refuse a start while closing, during a run, or without the MUT driving the shaft
        # forward (opposing torque on an undriven shaft would spin it backwards).
        if self._exiting:
            self.on_message("Start ignored: the runtime is closing.")
            return
        if self.phase != IDLE:
            self.on_message("Start ignored: a run is already active.")
            return
        if self._rpm is None or self._rpm < config.MIN_RUN_RPM:
            self.on_message(f"Start refused: encoder RPM must be at least {config.MIN_RUN_RPM:g} "
                            f"(MUT running forward).")
            return
        log = None
        try:
            if log_location is not None:
                log = RunLog(profile, log_location)   # before RUN, so a log failure cannot strand the drive
            self.vfd.set_torque_nm(0.0)
            self.vfd.run()                      # torque mode: no FWD/REV bits
        except Exception as e:
            self.on_message(f"[ERROR] Start failed: {e}")
            if log is not None:
                log.close()
            try:
                self.vfd.stop()                 # RUN may have reached the drive even if its reply was lost
            except Exception as stop_error:
                self.on_message(f"[ERROR] STOP after failed start also failed: {stop_error} "
                                f"- check the drive and use keypad STOP.")
            return

        # The drive is now in RUN at 0 N·m: reset the per-run state and start entering the profile.
        self._profile = profile
        self._torque_setpoint_nm = 0.0
        self._torque_in_force_nm = 0.0
        self._run_t0 = time.monotonic()
        self._profile_t0 = None
        self._stop_reason = None
        self._cycles = 0
        self._loop_dt_sum = 0.0
        self._overruns = 0
        self._elapsed = 0.0
        self._log = log
        self._log_ok = True
        self.phase = ENTERING
        self.on_message(f"RUN sent at 0 N·m. Entering: {profile.describe()}")

    def _begin_stop(self, reason):
        if self.phase in (ENTERING, RUNNING):
            self._stop_reason = reason
            self.phase = STOPPING
            self.on_message(f"Stopping ({reason}): ramping torque to 0 N·m.")

    def _comm_failure(self, error):
        self._comm_errors += 1
        # Rate-limited reporting, so a dead link does not flood the messages: report the
        # first failure, the one that triggers the shutdown below, then every 50th as a
        # "still failing" reminder (mainly in IDLE, where the count keeps climbing).
        if self._comm_errors in (1, config.MAX_CONSECUTIVE_COMM_ERRORS) or self._comm_errors % 50 == 0:
            self.on_message(f"[ERROR] Communication failure #{self._comm_errors}: {error}")
        if self._comm_errors >= config.MAX_CONSECUTIVE_COMM_ERRORS:
            self._begin_stop("Repeated communication errors")

    def _cycle(self):
        # Two timings per cycle, both logged:
        #   loop_dt     previous cycle's start -> this cycle's start: the loop period
        #               actually achieved, including the pacer's sleep and any request
        #               handling (e.g. a start's 0 N·m + RUN). Also the dt used for slewing.
        #   cycle_time  this cycle's start -> end of its Modbus work (reads + torque
        #               write), set further down: what the work costs. > period = overrun.
        # Within budget, loop_dt stays ~period however long the work takes, so only
        # cycle_time shows the headroom left. Over budget the pacer stops sleeping and
        # loop_dt ~ cycle_time.
        t_start = time.monotonic()
        loop_dt = t_start - self._last_cycle_t
        self._last_cycle_t = t_start
        comm_errors_before = self._comm_errors

        # 1-3. Encoder sample -> signed RPM, plus C2000 telemetry: one read_cycle(), three
        # Modbus transactions (block reads; with the torque write, four per cycle).
        # If a read fails the whole cycle is skipped: no torque write, no sample. The
        # drive keeps holding the last torque it accepted, and the next good encoder
        # sample gives the correct RPM over the longer, measured interval.
        try:
            enc, tel = self.vfd.read_cycle()
        except Exception as e:
            self._comm_failure(e)
            return

        if self._speed is None:
            self._speed = ShaftSpeedEstimator(enc)      # first sample only seeds the estimator
            return
        r = self._speed.update(enc)
        if r.plausible:
            self._rpm = r.rpm
        else:
            # Keep the last good RPM for the limit checks and the profile; stop the run.
            self.on_message(f"[WARN] Implausible encoder jump ({r.rpm:+.0f} rpm), sample discarded.")
            self._begin_stop("Implausible encoder data")
        self._check_limits()

        # 4-6. Profile -> limits -> torque setpoint in N·m. The driver converts it to
        # Pr.11-34 % and rounds to the register's 0.1 %, returning the N·m actually
        # written. The setpoint is written every cycle, even when unchanged.
        # _torque_in_force_nm is updated only if the write succeeds.
        target, limited, profile_time = self._update_torque_setpoint(loop_dt)
        if self.phase != IDLE:
            try:
                self._torque_in_force_nm = self.vfd.set_torque_nm(self._torque_setpoint_nm)
            except Exception as e:
                self._comm_failure(e)

        # Torque from the drive's output power P = power_vi (ω from the filtered encoder
        # speed). Near standstill P/ω is meaningless, hence the gate.
        #   electrical torque  P / ω: what the electrical power alone implies, i.e. after the
        #                      dyno motor's own losses.
        #   estimated torque   (P - losses) / ω, losses from config.NO_LOAD_LOSS_W at this
        #                      speed: P = T·ω + losses (P < 0 when regenerating), so this is
        #                      the motor's torque, the shaft torque the MUT works against.
        # Unlike the C2000 estimate, the estimated torque does NOT follow the command
        # exactly, and that is what makes it credible: it shows real torque transients,
        # e.g. the torque staying low while the flux builds up after RUN (~4 s) and the slow
        # settling at some torque levels (~40 s after RUN at -15 N·m, 2500 rpm), which the
        # drive's model value (torque_c2000_nm) never shows.
        torque_elec = estimated_torque = None
        if r.plausible and abs(r.rpm_filtered) >= config.MIN_RUN_RPM:
            power_w = tel.power_vi_kw * 1000.0
            loss_w = interp_clamped(abs(r.rpm_filtered), config.NO_LOAD_LOSS_RPM, config.NO_LOAD_LOSS_W)
            torque_elec = power_to_torque_nm(power_w, r.rpm_filtered)
            estimated_torque = power_to_torque_nm(power_w - loss_w, r.rpm_filtered)

        # 7-9. Timing, logging, publishing
        cycle_time = time.monotonic() - t_start      # reads + write only: logging/publishing not included
        if self.phase != IDLE and cycle_time > self._period:
            self._overruns += 1                      # counted before the Sample so it carries the new total
        sample = Sample(
            phase=self.phase,
            elapsed_time=None if self.phase == IDLE else t_start - self._run_t0,
            loop_dt=loop_dt,
            cycle_time=cycle_time,
            overruns=None if self.phase == IDLE else self._overruns,
            profile_time=profile_time,
            profile_target_nm=target,
            torque_command_nm=None if self.phase == IDLE else self._torque_in_force_nm,
            limited=limited,
            encoder_rpm=r.rpm if r.plausible else None,
            filtered_rpm=r.rpm_filtered,
            c2000_rpm=enc.c2000_rpm,
            torque_c2000_nm=tel.torque_nm,
            estimated_torque_nm=estimated_torque,
            torque_elec_nm=torque_elec,
            output_current_a=tel.output_current_a,
            output_voltage_v=tel.output_voltage_v,
            power_angle_deg=tel.power_angle_deg,
            power_vi_kw=tel.power_vi_kw,
            dc_bus_v=tel.dc_bus_v,
            drive_state=tel.state,
        )
        if self.phase != IDLE:
            # A logging failure (disk full, ...) must never stand between the drive and
            # STOP: report it once, stop logging this run and carry on.
            if self._log is not None and self._log_ok:
                try:
                    self._log.write(sample)
                except OSError as e:
                    self._log_ok = False
                    self.on_message(f"[ERROR] CSV logging stopped for this run: {e}")
            # The first cycle's loop_dt also contains the start request's extra writes
            # (0 N·m + RUN), so it is left out of the mean loop rate.
            if self._cycles > 0:
                self._loop_dt_sum += loop_dt
            self._cycles += 1
            self._elapsed = sample.elapsed_time
        self.on_sample(sample)

        # The stop ramp is done once the setpoint has reached 0 AND the drive has
        # accepted 0 (so a failed last write delays STOP rather than skipping 0 N·m).
        if self.phase == STOPPING and self._torque_setpoint_nm == 0.0 and self._torque_in_force_nm == 0.0:
            self._finish_run()

        # A run of consecutive comm failures ends only with a cycle in which every
        # transaction succeeded (reads, torque write, final 0 N·m + STOP). Resetting after
        # the reads alone would let a write that fails every cycle stay at 1 forever.
        if self._comm_errors == comm_errors_before:
            self._comm_errors = 0

    def _check_limits(self):
        if self.phase not in (ENTERING, RUNNING) or self._rpm is None:
            return
        if abs(self._rpm) > config.MAX_ABS_RPM:
            self._begin_stop(f"|RPM| {abs(self._rpm):.0f} above limit {config.MAX_ABS_RPM:g}")
        elif self._rpm < config.MIN_RUN_RPM:
            self._begin_stop(f"RPM {self._rpm:.0f} below {config.MIN_RUN_RPM:g}: MUT not driving the shaft")

    def _update_torque_setpoint(self, dt):
        """
        Updates self._torque_setpoint_nm for the current phase.
        Returns (target, limited, profile_time).

            IDLE      no torque is set
            STOPPING  slew toward 0
            ENTERING  evaluate the profile at t = 0, clamp, slew toward it; when the
                      setpoint arrives the profile clock starts (-> RUNNING), so the
                      profile shape itself is never distorted by the entry ramp
            RUNNING   evaluate the profile at its own elapsed time, clamp, use it
                      directly as the setpoint (steps in a profile stay steps)
        """
        max_step = config.TORQUE_SLEW_NM_PER_S * dt     # slew limit for this cycle's actual dt

        # Profile end is checked first, so the ramp-down starts in this same cycle.
        if self.phase == RUNNING:
            profile_time = time.monotonic() - self._profile_t0
            duration = self._profile.duration_s
            if duration is not None and profile_time >= duration:
                self._begin_stop("Profile complete")

        if self.phase == IDLE:
            return None, False, None
        if self.phase == STOPPING:
            self._torque_setpoint_nm = slew(self._torque_setpoint_nm, 0.0, max_step)
            return None, False, None

        if self.phase == ENTERING:
            profile_time = 0.0
        # Limits are applied to the raw profile target before any slewing.
        target = self._profile.evaluate(profile_time, self._rpm)
        clamped = min(max(target, config.TORQUE_LIMIT_MIN_NM), config.TORQUE_LIMIT_MAX_NM)

        if self.phase == ENTERING:
            self._torque_setpoint_nm = slew(self._torque_setpoint_nm, clamped, max_step)
            if self._torque_setpoint_nm == clamped:
                self.phase = RUNNING
                self._profile_t0 = time.monotonic()
                self.on_message("Profile started.")
                return target, clamped != target, 0.0
            return target, clamped != target, None

        self._torque_setpoint_nm = clamped
        return target, clamped != target, profile_time

    def _finish_run(self):
        """End of STOPPING: explicit 0 N·m, STOP, close the log, publish the result."""
        try:
            self.vfd.set_torque_nm(0.0)
            self.vfd.stop()
        except Exception as e:
            # Still STOPPING with the setpoint at 0, so _cycle calls this again next cycle.
            self._comm_failure(e)
            return
        if self._log is None:
            log_note = "Log: not saved"
        else:
            try:
                self._log.close()           # final flush can fail too (e.g. disk full)
            except OSError as e:
                self._log_ok = False
                self.on_message(f"[ERROR] CSV log could not be closed cleanly: {e}")
            incomplete = "" if self._log_ok else " (incomplete)"
            log_note = f"Log{incomplete}: {self._log.path}"
        self.phase = IDLE
        self.on_message(f"0 N·m written, STOP sent. {log_note}")
        self.on_run_finished(self._build_result())

    def _build_result(self):
        return RunResult(
            profile=self._profile.describe(),
            reason=self._stop_reason,
            log_path=None if self._log is None else self._log.path,
            duration_s=self._elapsed,
            cycles=self._cycles,
            mean_loop_dt=self._loop_dt_sum / (self._cycles - 1) if self._cycles > 1 else 0.0,
            overruns=self._overruns,
        )
