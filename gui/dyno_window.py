"""
DynoWindow: main window for running torque profiles on the dyno.

It talks only to a control.runtime_process.RuntimeProcess - the handle to the
control loop, which runs in its own process: start_run(profile, save_log),
request_stop() and poll(). The window never touches the drive.

One GUI timer drives everything: every UPDATE_MS it calls runtime.poll(), which
delivers the samples, messages and run results that arrived from the control
process by calling this window's handlers (in the GUI thread, so they may update
widgets directly), and then redraws the live plots. How long the GUI takes for any
of this has no effect on the control loop.
"""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit, QPushButton,
                               QSplitter, QTabWidget, QVBoxLayout, QWidget)

import config
from control.runtime import IDLE
from control.torque_profiles import PROFILES
from gui.profile_editor import ProfileEditor
from gui.telemetry_panel import TelemetryPanel
from gui.trace_plot import TracePlot

UPDATE_MS = 100        # GUI timer: take new events from the control process, then redraw live plots

# Live plots show a rolling window, so a redraw costs the same at any point in a run.
# The Results tab still plots the whole run. The window holds this many samples, so it
# spans LIVE_WINDOW_S at the nominal loop rate (longer if the loop runs slower).
LIVE_WINDOW_S = 30
LIVE_POINTS = int(LIVE_WINDOW_S * config.LOOP_RATE_HZ)

# Torque from the drive's electrical output power. Its offset must be stated wherever it is shown.
TORQUE_ELEC_POWER_TRACE = "Elec. P/ω estimate (incl. losses)"
TORQUE_ELEC_POWER_NOTE = ("Torque est. elec. P/ω = drive electrical output power / shaft speed. It includes "
                   "the dyno motor's losses, so it reads about losses/ω more positive than the shaft "
                   "torque (in regen: less negative). Blank below the min run RPM.")

TELEMETRY_FIELDS = [
    ("phase", "Phase"),
    ("drive_state", "Drive state"),
    ("encoder_rpm", "Encoder RPM"),
    ("c2000_rpm", "C2000 RPM"),
    ("torque_command", "Torque command"),
    ("torque_c2000", "Torque est. C2000"),
    ("torque_elec_power", "Torque est. elec. P/ω *"),
    ("current", "Current"),
    ("power", "Output power"),
    ("dc_bus", "DC bus"),
    ("loop_dt", "Loop dt"),
    ("elapsed", "Run time"),
    ("overruns", "Overruns (this run)"),
]


def fmt(value, spec, unit=""):
    return "-" if value is None else f"{value:{spec}}{unit}"


class DynoWindow(QMainWindow):
    def __init__(self, runtime):
        super().__init__()
        self.setWindowTitle("Dyno - Torque Runtime")
        self.runtime = runtime
        self._phase = IDLE
        self._close_pending = False

        # The runtime calls these from poll(), which only this window's timer calls,
        # so they always run in the GUI thread.
        runtime.on_sample = self._on_sample
        runtime.on_message = self._log
        runtime.on_run_finished = self._on_run_finished

        # ---- Left: controls, telemetry, messages ----
        self._editor = ProfileEditor(PROFILES)
        self._start = QPushButton("Start")
        self._stop = QPushButton("Stop")
        self._start.clicked.connect(self._on_start)
        self._stop.clicked.connect(lambda: self.runtime.request_stop("Stop pressed"))
        buttons = QHBoxLayout()
        buttons.addWidget(self._start)
        buttons.addWidget(self._stop)
        self._save_log = QCheckBox("Save CSV log")      # applies from the next Start; locked during a run
        self._save_log.setChecked(True)

        self._telemetry = TelemetryPanel("Telemetry", TELEMETRY_FIELDS)
        torque_elec_power_note = QLabel(f"<i>* {TORQUE_ELEC_POWER_NOTE}</i>")
        torque_elec_power_note.setWordWrap(True)
        self._messages = QPlainTextEdit(readOnly=True)
        self._messages.setMaximumBlockCount(500)

        limits = QLabel(
            f"Limits: torque {config.TORQUE_LIMIT_MIN_NM:.2f} .. {config.TORQUE_LIMIT_MAX_NM:.2f} N·m | "
            f"|RPM| <= {config.MAX_ABS_RPM:g} | RPM >= {config.MIN_RUN_RPM:g} while running"
        )
        limits.setWordWrap(True)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self._editor)
        left_layout.addLayout(buttons)
        left_layout.addWidget(self._save_log)
        left_layout.addWidget(limits)
        left_layout.addWidget(self._telemetry)
        left_layout.addWidget(torque_elec_power_note)
        left_layout.addWidget(QLabel("Messages"))
        left_layout.addWidget(self._messages, stretch=1)

        # ---- Right: live and result plots ----
        self._torque_plot = TracePlot("Torque", "time (s)", "torque (N·m)", max_points=LIVE_POINTS)
        self._torque_plot.add_trace("Command")
        self._torque_plot.add_trace("C2000 estimate")
        self._torque_plot.add_trace(TORQUE_ELEC_POWER_TRACE)
        self._speed_plot = TracePlot("Speed", "time (s)", "speed (rpm)", max_points=LIVE_POINTS)
        self._speed_plot.add_trace("Encoder RPM")
        self._speed_plot.add_trace("C2000 RPM", dashed=True)
        live = QWidget()
        live_layout = QVBoxLayout(live)
        live_layout.addWidget(self._torque_plot)
        live_layout.addWidget(self._speed_plot)

        self._summary = QLabel("No run yet.")
        self._summary.setWordWrap(True)
        self._compare_plot = TracePlot("Torque comparison", "time (s)", "torque (N·m)")
        self._compare_plot.add_trace("Command")
        self._compare_plot.add_trace("C2000 estimate")
        self._compare_plot.add_trace(TORQUE_ELEC_POWER_TRACE)
        self._error_plot = TracePlot("Torque error (estimate - command)", "time (s)", "error (N·m)")
        self._error_plot.add_trace("Error", color="#d62728")
        results = QWidget()
        results_layout = QVBoxLayout(results)
        results_layout.addWidget(self._summary)
        results_layout.addWidget(self._compare_plot)
        results_layout.addWidget(self._error_plot)

        self._tabs = QTabWidget()
        self._tabs.addTab(live, "Live")
        self._tabs.addTab(results, "Results")

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(self._tabs)
        splitter.setStretchFactor(1, 1)
        self.setCentralWidget(splitter)
        self.resize(1300, 850)

        # Live plots are redrawn on this timer, not on every sample: drawing cost stays
        # fixed whatever the loop rate.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start(UPDATE_MS)
        self._update_buttons()

    # --------------------------------------------------------------------------
    # User actions
    # --------------------------------------------------------------------------
    def _on_start(self):
        try:
            profile = self._editor.profile()
        except ValueError as e:
            self._log(f"[ERROR] {e}")
            return
        self._torque_plot.clear()
        self._speed_plot.clear()
        self._tabs.setCurrentIndex(0)
        self.runtime.start_run(profile, save_log=self._save_log.isChecked())

    def closeEvent(self, event):
        # Two-step close during a run: the first close asks the runtime to stop and keeps
        # the window open; _on_run_finished closes it once STOP has been sent. A second
        # close (e.g. if the drive stopped responding) closes immediately; the script
        # then still calls runtime.close(), which keeps trying to finish the shutdown.
        if self.runtime.active and not self._close_pending:
            self._close_pending = True
            self.runtime.request_stop("Window closed")
            self._log("Stopping the run; the window closes after STOP (close again to force).")
            event.ignore()
            return
        event.accept()

    # --------------------------------------------------------------------------
    # Runtime events (called from runtime.poll(), in the GUI thread)
    # --------------------------------------------------------------------------
    def _on_sample(self, s):
        # Every sample carries the runtime phase, so the buttons follow the runtime
        # without the GUI reading its state directly.
        self._phase = s.phase
        self._update_buttons()

        if s.phase != IDLE:
            self._torque_plot.append("Command", s.elapsed_time, s.torque_command_nm)
            self._torque_plot.append("C2000 estimate", s.elapsed_time, s.torque_c2000_nm)
            self._torque_plot.append(TORQUE_ELEC_POWER_TRACE, s.elapsed_time, s.torque_elec_power_nm)
            self._speed_plot.append("Encoder RPM", s.elapsed_time, s.encoder_rpm)
            self._speed_plot.append("C2000 RPM", s.elapsed_time, s.c2000_rpm)

        self._telemetry.set_values({
            "phase": s.phase,
            "drive_state": s.drive_state,
            "encoder_rpm": fmt(s.encoder_rpm, "+.1f", " rpm"),
            "c2000_rpm": fmt(s.c2000_rpm, "d", " rpm"),
            "torque_command": fmt(s.torque_command_nm, "+.2f", " N·m") + (" (limited)" if s.limited else ""),
            "torque_c2000": fmt(s.torque_c2000_nm, "+.2f", " N·m"),
            "torque_elec_power": fmt(s.torque_elec_power_nm, "+.2f", " N·m"),
            "current": fmt(s.current_a, ".2f", " A"),
            "power": fmt(s.output_power_kw, "+.1f", " kW"),
            "dc_bus": fmt(s.dc_bus_v, ".1f", " V"),
            "loop_dt": fmt(s.loop_dt * 1000, ".0f", " ms"),
            "elapsed": fmt(s.elapsed_time, ".1f", " s"),
            "overruns": fmt(s.overruns, "d"),
        })

    def _on_run_finished(self, r):
        self._phase = IDLE
        self._update_buttons()
        self._telemetry.set_values({"phase": IDLE})
        times = [s.elapsed_time for s in r.samples]
        self._compare_plot.clear()
        self._compare_plot.set_data("Command", times, [s.torque_command_nm for s in r.samples])
        self._compare_plot.set_data("C2000 estimate", times, [s.torque_c2000_nm for s in r.samples])
        self._compare_plot.set_data(TORQUE_ELEC_POWER_TRACE, times, [s.torque_elec_power_nm for s in r.samples])
        self._error_plot.clear()
        self._error_plot.set_data("Error", r.error_times, r.errors)
        self._compare_plot.refresh()
        self._error_plot.refresh()

        loop_hz = 1.0 / r.mean_loop_dt if r.mean_loop_dt else 0.0
        if r.stats:
            stats = (f"<b>Bias (mean error):</b> {r.stats.bias:+.2f} N·m &nbsp; "
                     f"<b>Mean absolute error:</b> {r.stats.mae:.2f} N·m &nbsp; "
                     f"<b>Max absolute error:</b> {r.stats.max_abs:.2f} N·m &nbsp; ({r.stats.n} samples)")
        else:
            stats = "<b>No RUNNING-phase data</b> - statistics not available."
        self._summary.setText(
            f"<b>{r.profile}</b><br>"
            f"Stopped: {r.reason} | Duration {r.duration_s:.1f} s | Mean loop rate {loop_hz:.1f} Hz | "
            f"Overruns {r.overruns}/{len(r.samples)} cycles<br>"
            f"CSV: {r.log_path or 'not saved'}<br>{stats}<br>"
            f"<i>Error = C2000 estimate - command in force when read, over the RUNNING phase. "
            f"The C2000 estimate is the drive's internal model, not an independent torque measurement. "
            f"{TORQUE_ELEC_POWER_TRACE} is plotted but not scored: {TORQUE_ELEC_POWER_NOTE}</i>"
        )
        self._tabs.setCurrentIndex(1)
        if self._close_pending:
            self.close()

    # --------------------------------------------------------------------------
    # Helpers
    # --------------------------------------------------------------------------
    def _log(self, text):
        self._messages.appendPlainText(text)

    def _update_buttons(self):
        idle = self._phase == IDLE
        self._start.setEnabled(idle)
        self._stop.setEnabled(not idle)
        self._editor.setEnabled(idle)
        self._save_log.setEnabled(idle)

    def _on_timer(self):
        self.runtime.poll()           # calls _on_sample / _log / _on_run_finished for new events
        self._torque_plot.refresh()
        self._speed_plot.refresh()
