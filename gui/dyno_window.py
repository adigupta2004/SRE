"""
DynoWindow: main window for running torque profiles on the dyno.

It talks only to a control.runtime.DynoRuntime: start_run(profile) and
request_stop(). Samples, messages and run results arrive from the runtime's
worker thread through RuntimeBridge signals (queued into the GUI thread), so the
control loop never waits for the GUI. Plots are redrawn on a GUI timer.
"""

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit, QPushButton,
                               QSplitter, QTabWidget, QVBoxLayout, QWidget)

import config
from control.runtime import IDLE
from control.torque_profiles import PROFILES
from gui.profile_editor import ProfileEditor
from gui.telemetry_panel import TelemetryPanel
from gui.trace_plot import TracePlot

PLOT_REFRESH_MS = 100

TELEMETRY_FIELDS = [
    ("phase", "Phase"),
    ("drive_state", "Drive state"),
    ("encoder_rpm", "Encoder RPM"),
    ("c2000_rpm", "C2000 RPM"),
    ("torque_command", "Torque command"),
    ("torque_estimate", "Torque estimate"),
    ("current", "Current"),
    ("power", "Output power"),
    ("dc_bus", "DC bus"),
    ("loop_dt", "Loop dt"),
    ("elapsed", "Run time"),
    ("overruns", "Overruns (this run)"),
]


class RuntimeBridge(QObject):
    """
    Carries runtime callbacks (worker thread) into the GUI thread.

    The runtime's callbacks are set to these signals' emit methods. The bridge
    lives in the GUI thread, so Qt queues each emit and runs the connected slot
    later in the GUI thread: emit returns immediately and the control loop never
    waits for drawing. Qt widgets must only be touched from the GUI thread, so
    the slots are the only place samples meet widgets.
    """
    sample = Signal(object)
    message = Signal(str)
    run_finished = Signal(object)


def fmt(value, spec, unit=""):
    return "-" if value is None else f"{value:{spec}}{unit}"


class DynoWindow(QMainWindow):
    def __init__(self, runtime):
        super().__init__()
        self.setWindowTitle("Dyno - Torque Runtime")
        self.runtime = runtime
        self._phase = IDLE
        self._overruns = 0
        self._close_pending = False

        self._bridge = RuntimeBridge()
        self._bridge.sample.connect(self._on_sample)
        self._bridge.message.connect(self._log)
        self._bridge.run_finished.connect(self._on_run_finished)
        # From here on the runtime publishes through the bridge (see RuntimeBridge).
        runtime.on_sample = self._bridge.sample.emit
        runtime.on_message = self._bridge.message.emit
        runtime.on_run_finished = self._bridge.run_finished.emit

        # ---- Left: controls, telemetry, messages ----
        self._editor = ProfileEditor(PROFILES)
        self._start = QPushButton("Start")
        self._stop = QPushButton("Stop")
        self._start.clicked.connect(self._on_start)
        self._stop.clicked.connect(lambda: self.runtime.request_stop("Stop pressed"))
        buttons = QHBoxLayout()
        buttons.addWidget(self._start)
        buttons.addWidget(self._stop)

        self._telemetry = TelemetryPanel("Telemetry", TELEMETRY_FIELDS)
        self._messages = QPlainTextEdit(readOnly=True)
        self._messages.setMaximumBlockCount(500)

        limits = QLabel(
            f"Limits: torque {config.TORQUE_LIMIT_MIN_PCT:g} .. {config.TORQUE_LIMIT_MAX_PCT:g} % | "
            f"|RPM| <= {config.MAX_ABS_RPM:g} | RPM >= {config.MIN_RUN_RPM:g} while running"
        )
        limits.setWordWrap(True)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self._editor)
        left_layout.addLayout(buttons)
        left_layout.addWidget(limits)
        left_layout.addWidget(self._telemetry)
        left_layout.addWidget(QLabel("Messages"))
        left_layout.addWidget(self._messages, stretch=1)

        # ---- Right: live and result plots ----
        self._torque_plot = TracePlot("Torque", "time (s)", "torque (%)")
        self._torque_plot.add_trace("Command")
        self._torque_plot.add_trace("C2000 estimate")
        self._speed_plot = TracePlot("Speed", "time (s)", "speed (rpm)")
        self._speed_plot.add_trace("Encoder RPM")
        self._speed_plot.add_trace("C2000 RPM", dashed=True)
        live = QWidget()
        live_layout = QVBoxLayout(live)
        live_layout.addWidget(self._torque_plot)
        live_layout.addWidget(self._speed_plot)

        self._summary = QLabel("No run yet.")
        self._summary.setWordWrap(True)
        self._compare_plot = TracePlot("Torque comparison", "time (s)", "torque (%)")
        self._compare_plot.add_trace("Command")
        self._compare_plot.add_trace("C2000 estimate")
        self._error_plot = TracePlot("Torque error (estimate - command)", "time (s)", "error (%)")
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
        self._timer.timeout.connect(self._refresh_plots)
        self._timer.start(PLOT_REFRESH_MS)
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
        self._overruns = 0
        self._tabs.setCurrentIndex(0)
        self.runtime.start_run(profile)

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
    # Runtime events (GUI thread)
    # --------------------------------------------------------------------------
    def _on_sample(self, s):
        # Every sample carries the runtime phase, so the buttons follow the runtime
        # without the GUI reading its state directly.
        self._phase = s.phase
        self._update_buttons()

        if s.phase != IDLE:
            if s.cycle_time > 1.0 / config.LOOP_RATE_HZ:
                self._overruns += 1
            self._torque_plot.append("Command", s.elapsed_time, s.torque_command_pct)
            self._torque_plot.append("C2000 estimate", s.elapsed_time, s.torque_estimate_pct)
            self._speed_plot.append("Encoder RPM", s.elapsed_time, s.encoder_rpm)
            self._speed_plot.append("C2000 RPM", s.elapsed_time, s.c2000_rpm)

        self._telemetry.set_values({
            "phase": s.phase,
            "drive_state": s.drive_state,
            "encoder_rpm": fmt(s.encoder_rpm, "+.1f", " rpm"),
            "c2000_rpm": fmt(s.c2000_rpm, "d", " rpm"),
            "torque_command": fmt(s.torque_command_pct, "+.1f", " %") + (" (limited)" if s.limited else ""),
            "torque_estimate": fmt(s.torque_estimate_pct, "+.1f", " %"),
            "current": fmt(s.current_a, ".2f", " A"),
            "power": fmt(s.output_power_kw, "+.1f", " kW"),
            "dc_bus": fmt(s.dc_bus_v, ".1f", " V"),
            "loop_dt": fmt(s.loop_dt * 1000, ".0f", " ms"),
            "elapsed": fmt(s.elapsed_time, ".1f", " s"),
            "overruns": str(self._overruns) if s.phase != IDLE else "-",
        })

    def _on_run_finished(self, r):
        self._phase = IDLE
        self._update_buttons()
        self._telemetry.set_values({"phase": IDLE})
        times = [s.elapsed_time for s in r.samples]
        self._compare_plot.clear()
        self._compare_plot.set_data("Command", times, [s.torque_command_pct for s in r.samples])
        self._compare_plot.set_data("C2000 estimate", times, [s.torque_estimate_pct for s in r.samples])
        self._error_plot.clear()
        self._error_plot.set_data("Error", r.error_times, r.errors)
        self._compare_plot.refresh()
        self._error_plot.refresh()

        loop_hz = 1.0 / r.mean_loop_dt if r.mean_loop_dt else 0.0
        if r.stats:
            stats = (f"<b>Bias (mean error):</b> {r.stats.bias:+.2f} % &nbsp; "
                     f"<b>Mean absolute error:</b> {r.stats.mae:.2f} % &nbsp; "
                     f"<b>Max absolute error:</b> {r.stats.max_abs:.2f} % &nbsp; ({r.stats.n} samples)")
        else:
            stats = "<b>No RUNNING-phase data</b> - statistics not available."
        self._summary.setText(
            f"<b>{r.profile}</b><br>"
            f"Stopped: {r.reason} | Duration {r.duration_s:.1f} s | Mean loop rate {loop_hz:.1f} Hz | "
            f"Overruns {r.overruns}/{len(r.samples)} cycles<br>"
            f"CSV: {r.log_path}<br>{stats}<br>"
            f"<i>Error = C2000 estimate - command in force when read, over the RUNNING phase. "
            f"The C2000 estimate is the drive's internal model, not an independent torque measurement.</i>"
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

    def _refresh_plots(self):
        self._torque_plot.refresh()
        self._speed_plot.refresh()
