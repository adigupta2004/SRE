"""
Dyno runtime: the reusable ~10 Hz control loop.

A worker thread owns the C2000 while the runtime is open. Every cycle it:
    reads the encoder sample and C2000 telemetry -> signed shaft RPM
    evaluates the active torque profile (actual elapsed time, actual RPM)
    clamps to the commissioning torque limits -> writes the torque command
    measures loop timing -> logs the sample -> publishes it
and paces itself with RatePacer. Nothing here waits on a GUI: results are
published through callbacks, which must return quickly (e.g. emit a Qt signal).

Phases:
    IDLE      drive STOPPED, read-only monitoring (no torque writes, no logging)
    ENTERING  RUN sent at 0 N·m; command slews to the profile's starting target
    RUNNING   profile active; its clock starts when ENTERING completes
    STOPPING  command slews to 0, then 0 N·m is written explicitly and STOP sent

Controlled shutdown (-> STOPPING) on: Stop request, profile end, |RPM| above
MAX_ABS_RPM, RPM below MIN_RUN_RPM, implausible encoder data, repeated comm
errors, or an unexpected error in the loop.
"""

import queue
import threading
import time
from dataclasses import dataclass

import config
from control.run_log import RunLog
from control.validation import error_stats, torque_error
from hardware.encoder import ShaftSpeedEstimator
from utils.mathutils import power_to_torque_nm
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
    torque_c2000_nm: float        # 2208H in N·m: the drive's own torque estimate, signed
    torque_elec_power_nm: float   # drive ELECTRICAL output power / filtered shaft speed (P/ω), signed.
                                  # Includes the dyno motor's losses, so it is offset by about
                                  # +losses/ω vs the shaft torque (in regen: less negative).
                                  # None below MIN_RUN_RPM or with implausible encoder data.
    current_a: float
    output_power_kw: float        # signed, negative = regenerating
    dc_bus_v: float
    drive_state: str              # 2101H state, e.g. "Operating"


@dataclass(frozen=True)
class RunResult:
    profile: str                  # profile description
    reason: str                   # why the run stopped
    log_path: str                 # None if the run was not logged to CSV
    samples: list                 # every Sample of the run (ENTERING .. STOPPING)
    error_times: list             # RUNNING-phase torque error (estimate - command in force)
    errors: list
    stats: object                 # validation.ErrorStats, or None if no RUNNING data
    duration_s: float
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


class DynoRuntime:
    """
    Threading model:
      - After start(), only the worker thread talks to the C2000 and only it reads
        or changes the per-run state below.
      - Other threads (e.g. the GUI) only call start_run() / request_stop() /
        close(), which just put requests on a queue, and may read `phase`.
      - The worker applies queued requests at the start of each cycle, so no
        locks are needed.
      - Callbacks run in the worker thread; they must be quick and must not touch
        the drive (the GUI turns them into Qt signals).
    """

    def __init__(self, vfd, on_sample=None, on_message=None, on_run_finished=None):
        self.vfd = vfd
        # Callbacks: the runtime's only output, called from the worker thread.
        #   on_sample(Sample)          every successful cycle, IDLE included (~10 Hz)
        #   on_message(str)            events: start/stop, refusals, warnings, errors
        #   on_run_finished(RunResult) once per run, after 0 N·m + STOP
        # Any not given becomes a no-op, so the code can always call them unchecked.
        # They are public so a GUI can plug in after construction (DynoWindow sets them
        # to Qt signal emits, which queue the data to the GUI thread and return at once).
        # Set them before start(). Each must return quickly: its time adds to the cycle.
        self.on_sample = on_sample or (lambda sample: None)
        self.on_message = on_message or (lambda text: None)
        self.on_run_finished = on_run_finished or (lambda result: None)

        self.phase = IDLE
        self._requests = queue.Queue()
        self._thread = threading.Thread(target=self._worker, name="dyno-runtime", daemon=True)
        self._period = 1.0 / config.LOOP_RATE_HZ
        self._speed = None            # ShaftSpeedEstimator, seeded by the first sample
        self._rpm = None              # last plausible raw encoder RPM
        self._comm_errors = 0
        self._last_cycle_t = None
        self._exiting = False

        # Per-run state. Two torque values are kept on purpose:
        #   _command           what we want the drive to have: the unrounded value the
        #                      slew/profile logic works on (rounding it every cycle
        #                      could stall a slow slew).
        #   _command_in_force  what the drive actually has: the last value whose
        #                      write succeeded. Logs, plots and the torque-error
        #                      statistics use this, so a failed write never shows up
        #                      as a torque the drive did not receive.
        self._profile = None
        self._command = 0.0           # unrounded torque command (N·m)
        self._command_in_force = 0.0  # last torque command the drive accepted (N·m, after register rounding)
        self._run_t0 = None
        self._profile_t0 = None
        self._stop_reason = None
        self._log = None
        self._log_ok = True           # False once a CSV write failed: the rest of the run is unlogged
        self._samples = []
        self._overruns = 0

    # --------------------------------------------------------------------------
    # Thread-safe API
    # --------------------------------------------------------------------------
    def start(self):
        """
        Commands 0 N·m and STOP, checks that the drive's torque scaling matches
        config (Pr.05-02, Pr.05-03, Pr.11-27), then starts the worker in IDLE.
        Raises on comm failure or a scaling mismatch (ValueError); the drive is
        then left at 0 + STOP. Every user of the runtime gets this check.
        """
        self.vfd.set_torque_nm(0.0)
        self.vfd.stop()
        self.vfd.verify_torque_scaling()
        self._thread.start()

    def start_run(self, profile, save_log=True):
        """Requests a run; save_log=False runs it without a CSV file (results are still kept)."""
        self._requests.put(("start", (profile, save_log)))

    def request_stop(self, reason="Stop requested"):
        self._requests.put(("stop", reason))

    def close(self, timeout=None):
        """
        Stops any active run through the normal shutdown path, then ends the
        worker. Returns True if the worker finished within the timeout.
        """
        if timeout is None:
            max_command_nm = max(abs(config.TORQUE_LIMIT_MIN_NM), abs(config.TORQUE_LIMIT_MAX_NM))
            max_ramp_down_s = max_command_nm / config.TORQUE_SLEW_NM_PER_S
            timeout = max_ramp_down_s + 10.0      # margin: final 0 N·m + STOP, comm retries
        self._requests.put(("exit", None))
        if self._thread.is_alive():
            self._thread.join(timeout)
        return not self._thread.is_alive()

    @property
    def active(self):
        return self.phase != IDLE

    # --------------------------------------------------------------------------
    # Worker
    # --------------------------------------------------------------------------
    def _worker(self):
        # Each iteration: wait for the next slot -> apply queued requests -> one cycle.
        pacer = RatePacer(config.LOOP_RATE_HZ)
        self._last_cycle_t = time.monotonic()
        while True:
            pacer.wait()
            try:
                self._handle_requests()
                # An exit request first moves an active run to STOPPING; the thread only
                # ends once that has finished (back in IDLE), so closing always goes
                # through ramp -> 0 N·m -> STOP.
                if self._exiting and self.phase == IDLE:
                    return
                self._cycle()
            except Exception as e:
                # Never let the loop die with the drive running.
                self.on_message(f"[ERROR] Unexpected runtime error: {e!r}")
                self._begin_stop("Unexpected runtime error")

    def _handle_requests(self):
        """Applies queued requests (an exit request sets self._exiting)."""
        while True:
            try:
                kind, arg = self._requests.get_nowait()
            except queue.Empty:
                return
            if kind == "start":
                self._start_run(*arg)
            elif kind == "stop":
                self._begin_stop(arg)
            elif kind == "exit":
                self._exiting = True
                self._begin_stop("Runtime closing")

    def _start_run(self, profile, save_log):
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
            if save_log:
                log = RunLog(profile)           # before RUN, so a log failure cannot strand the drive
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
        self._command = 0.0
        self._command_in_force = 0.0
        self._run_t0 = time.monotonic()
        self._profile_t0 = None
        self._stop_reason = None
        self._samples = []
        self._overruns = 0
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

        # 1-3. Encoder sample -> signed RPM, then C2000 telemetry.
        # If a read fails the whole cycle is skipped: no torque write, no sample. The
        # drive keeps holding the last command it accepted, and the next good encoder
        # sample gives the correct RPM over the longer, measured interval.
        try:
            enc = self.vfd.read_encoder_sample()
            tel = self.vfd.read_telemetry()
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

        # 4-6. Profile -> limits -> torque command in N·m. The driver converts it to
        # Pr.11-34 % and rounds to the register's 0.1 %, returning the N·m actually
        # commanded. The command is written every cycle, even when unchanged.
        # _command_in_force is updated only if the write succeeds.
        target, limited, profile_time = self._next_command(loop_dt)

        # Torque from the drive's electrical output power, T = P / ω (ω from the filtered
        # encoder speed). P is measured at the drive output, before the dyno motor's
        # losses, so this reads about losses/ω more positive than the shaft torque
        # (in regen, less negative). Near standstill P/ω is meaningless, hence the gate.
        torque_elec_power = None
        if r.plausible and abs(r.rpm_filtered) >= config.MIN_RUN_RPM:
            torque_elec_power = power_to_torque_nm(tel.output_power_kw * 1000.0, r.rpm_filtered)
        if self.phase != IDLE:
            try:
                self._command_in_force = self.vfd.set_torque_nm(self._command)
            except Exception as e:
                self._comm_failure(e)

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
            torque_command_nm=None if self.phase == IDLE else self._command_in_force,
            limited=limited,
            encoder_rpm=r.rpm if r.plausible else None,
            filtered_rpm=r.rpm_filtered,
            c2000_rpm=enc.c2000_rpm,
            torque_c2000_nm=tel.torque_nm,
            torque_elec_power_nm=torque_elec_power,
            current_a=tel.current_a,
            output_power_kw=tel.output_power_kw,
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
            self._samples.append(sample)
        self.on_sample(sample)

        # The stop ramp is done once the target has reached 0 AND the drive has
        # accepted 0 (so a failed last write delays STOP rather than skipping 0 N·m).
        if self.phase == STOPPING and self._command == 0.0 and self._command_in_force == 0.0:
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

    def _next_command(self, dt):
        """
        Updates self._command for the current phase. Returns (target, limited, profile_time).

            IDLE      nothing is commanded
            STOPPING  slew toward 0
            ENTERING  evaluate the profile at t = 0, clamp, slew toward it; when the
                      command arrives the profile clock starts (-> RUNNING), so the
                      profile shape itself is never distorted by the entry ramp
            RUNNING   evaluate the profile at its own elapsed time, clamp, command it
                      directly (steps in a profile stay steps)
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
            self._command = slew(self._command, 0.0, max_step)
            return None, False, None

        if self.phase == ENTERING:
            profile_time = 0.0
        # Limits are applied to the raw profile target before any slewing.
        target = self._profile.evaluate(profile_time, self._rpm)
        clamped = min(max(target, config.TORQUE_LIMIT_MIN_NM), config.TORQUE_LIMIT_MAX_NM)

        if self.phase == ENTERING:
            self._command = slew(self._command, clamped, max_step)
            if self._command == clamped:
                self.phase = RUNNING
                self._profile_t0 = time.monotonic()
                self.on_message("Profile started.")
                return target, clamped != target, 0.0
            return target, clamped != target, None

        self._command = clamped
        return target, clamped != target, profile_time

    def _finish_run(self):
        """End of STOPPING: explicit 0 N·m, STOP, close the log, publish the result."""
        try:
            self.vfd.set_torque_nm(0.0)
            self.vfd.stop()
        except Exception as e:
            # Still STOPPING with the command at 0, so _cycle calls this again next cycle.
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
        samples = self._samples
        # Error statistics cover the RUNNING phase and the C2000 estimate only: the entry
        # and stop ramps, and the electrical-power estimate (offset by motor losses), are
        # plotted but not scored.
        running = [s for s in samples if s.phase == RUNNING]
        error_times, errors = torque_error(
            [s.elapsed_time for s in running],
            [s.torque_command_nm for s in running],
            [s.torque_c2000_nm for s in running],
        )
        # The first sample's loop_dt also contains the start request's extra writes
        # (0 N·m + RUN), so it is left out of the mean loop rate.
        loop_dts = [s.loop_dt for s in samples[1:]]
        return RunResult(
            profile=self._profile.describe(),
            reason=self._stop_reason,
            log_path=None if self._log is None else self._log.path,
            samples=samples,
            error_times=error_times,
            errors=errors,
            stats=error_stats(errors),
            duration_s=samples[-1].elapsed_time if samples else 0.0,
            mean_loop_dt=sum(loop_dts) / len(loop_dts) if loop_dts else 0.0,
            overruns=self._overruns,
        )
