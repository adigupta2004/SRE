"""
DynoWindow: main window for running torque profiles on the dyno, and a viewer for
run logs.

It talks only to a control.runtime_process.RuntimeProcess - the handle to the
control loop, which runs in its own process: start_run(profile, log_location),
request_stop() and poll(). The window never touches the drive. Without a runtime
(drive not connected) it opens as a log viewer: running is disabled, and the
Results tab can still open any saved run log.

The Results tab is a LogView (gui/log_view.py), which owns everything about
results: this window only hands it each finished run.

One GUI timer drives everything: every UPDATE_MS it calls runtime.poll(), which
delivers the samples, messages and run results that arrived from the control
process by calling this window's handlers (in the GUI thread, so they may update
widgets directly), and then redraws the live plots. How long the GUI takes for any
of this has no effect on the control loop.
"""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPlainTextEdit, QPushButton,
                               QSplitter, QTabWidget, QVBoxLayout, QWidget)

import config
from control.runtime import IDLE
from control.torque_profiles import PROFILES
from gui.log_view import LogView
from gui.quantities import QUANTITIES
from gui.profile_editor import ProfileEditor
from gui.telemetry_panel import TelemetryPanel
from gui.trace_plot import TracePlot

UPDATE_MS = 100        # GUI timer: take new events from the control process, then redraw live plots

# Live plots show a rolling window, so a redraw costs the same at any point in a run.
# The Results tab still plots the whole run. The window holds this many samples, so it
# spans LIVE_WINDOW_S at the nominal loop rate (longer if the loop runs slower).
LIVE_WINDOW_S = 30
LIVE_POINTS = int(LIVE_WINDOW_S * config.LOOP_RATE_HZ)

TELEMETRY_FIELDS = [
    ("phase", "Phase"),
    ("drive_state", "Drive state"),
    ("encoder_rpm", "Encoder RPM"),
    ("c2000_rpm", "C2000 RPM"),
    ("torque_command", "Torque command"),
    ("estimated_torque", "Estimated torque"),
    ("torque_c2000", "C2000 torque estimate"),
    ("torque_elec", "Electrical torque P/ω"),
    ("current", "Output current"),
    ("voltage", "Output voltage"),
    ("power_angle", "Power angle"),
    ("power", "Output power"),
    ("dc_bus", "DC bus"),
    ("loop_dt", "Loop dt"),
    ("elapsed", "Run time"),
    ("overruns", "Overruns (this run)"),
]


def fmt(value, spec, unit=""):
    return "-" if value is None else f"{value:{spec}}{unit}"


class DynoWindow(QMainWindow):
    def __init__(self, runtime, startup_error=None):
        """runtime: a started RuntimeProcess, or None to open as a log viewer only."""
        super().__init__()
        self.setWindowTitle("Regenerative Dynamometer" if runtime
                            else "Regenerative Dynamometer - Log Viewer (drive not connected)")
        self.runtime = runtime
        self._phase = IDLE
        self._close_pending = False

        # The runtime calls these from poll(), which only this window's timer calls,
        # so they always run in the GUI thread.
        if runtime is not None:
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
        # CSV logging: applies from the next Start; locked during a run. Each run's CSV
        # goes into a "logs" folder inside the chosen location (default: project root).
        self._save_log = QCheckBox("Save CSV log")
        self._save_log.setChecked(True)
        self._save_log.toggled.connect(lambda _: self._update_buttons())
        self._log_location = QLineEdit(readOnly=True)
        self._browse = QPushButton("Browse…")
        self._browse.clicked.connect(self._on_browse)
        self._set_log_location(config.DEFAULT_LOG_LOCATION)
        log_row = QHBoxLayout()       # [x] Save CSV log ......... [Browse…]; full path on the line below
        log_row.addWidget(self._save_log)
        log_row.addStretch()
        log_row.addWidget(self._browse)

        self._telemetry = TelemetryPanel("Telemetry", TELEMETRY_FIELDS)
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
        left_layout.addLayout(log_row)
        left_layout.addWidget(self._log_location)
        left_layout.addWidget(limits)
        left_layout.addWidget(self._telemetry)
        left_layout.addWidget(QLabel("Messages"))
        left_layout.addWidget(self._messages, stretch=1)

        # ---- Right: live and result plots ----
        # One live plot with a checkbox per quantity (the same list as the Results tab).
        # Every quantity is recorded whether or not it is shown, so ticking one shows
        # its last LIVE_WINDOW_S at once.
        self._live_plot = TracePlot(f"Live (last {LIVE_WINDOW_S} s)", "time (s)", "", max_points=LIVE_POINTS)
        for q in QUANTITIES:
            self._live_plot.add_trace(q.label, color=q.color, visible=q.shown)

        self._results = LogView()

        self._tabs = QTabWidget()
        self._tabs.addTab(self._live_plot, "Live")
        self._tabs.addTab(self._results, "Results")

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
        if startup_error:
            self._log(f"[ERROR] Drive not connected: {startup_error}")
            self._log("Log viewer only: use Open CSV… on the Results tab. "
                      "Restart main.py to run the dyno.")
            self._tabs.setCurrentIndex(1)

    # --------------------------------------------------------------------------
    # User actions
    # --------------------------------------------------------------------------
    def _on_start(self):
        try:
            profile = self._editor.profile()
        except ValueError as e:
            self._log(f"[ERROR] {e}")
            return
        self._live_plot.clear()
        self._tabs.setCurrentIndex(0)
        log_location = self._log_location.text() if self._save_log.isChecked() else None
        self.runtime.start_run(profile, log_location)

    def _on_browse(self):
        location = QFileDialog.getExistingDirectory(
            self, "Choose where to create the logs folder", self._log_location.text())
        if location:                  # empty if the dialog was cancelled
            self._set_log_location(location)

    def closeEvent(self, event):
        # Two-step close during a run: the first close asks the runtime to stop and keeps
        # the window open; _on_run_finished closes it once STOP has been sent. A second
        # close (e.g. if the drive stopped responding) closes immediately; the script
        # then still calls runtime.close(), which keeps trying to finish the shutdown.
        if self.runtime is not None and self.runtime.active and not self._close_pending:
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
            for q in QUANTITIES:
                self._live_plot.append(q.label, s.elapsed_time, getattr(s, q.column))

        self._telemetry.set_values({
            "phase": s.phase,
            "drive_state": s.drive_state,
            "encoder_rpm": fmt(s.encoder_rpm, "+.1f", " rpm"),
            "c2000_rpm": fmt(s.c2000_rpm, "d", " rpm"),
            "torque_command": fmt(s.torque_command_nm, "+.2f", " N·m") + (" (limited)" if s.limited else ""),
            "estimated_torque": fmt(s.estimated_torque_nm, "+.2f", " N·m"),
            "torque_c2000": fmt(s.torque_c2000_nm, "+.2f", " N·m"),
            "torque_elec": fmt(s.torque_elec_nm, "+.2f", " N·m"),
            "current": fmt(s.output_current_a, ".2f", " A"),
            "voltage": fmt(s.output_voltage_v, ".1f", " V"),
            "power_angle": fmt(s.power_angle_deg, "+.1f", "°"),
            "power": fmt(s.power_vi_kw, "+.2f", " kW"),
            "dc_bus": fmt(s.dc_bus_v, ".1f", " V"),
            "loop_dt": fmt(s.loop_dt * 1000, ".0f", " ms"),
            "elapsed": fmt(s.elapsed_time, ".1f", " s"),
            "overruns": fmt(s.overruns, "d"),
        })

    def _on_run_finished(self, r):
        self._phase = IDLE
        self._update_buttons()
        self._telemetry.set_values({"phase": IDLE})
        self._results.show_run(r)
        self._tabs.setCurrentIndex(1)
        if self._close_pending:
            self.close()

    # --------------------------------------------------------------------------
    # Helpers
    # --------------------------------------------------------------------------
    def _log(self, text):
        self._messages.appendPlainText(text)

    def _update_buttons(self):
        connected = self.runtime is not None      # without a drive: viewer only
        idle = self._phase == IDLE
        self._start.setEnabled(connected and idle)
        self._stop.setEnabled(connected and not idle)
        self._editor.setEnabled(connected and idle)
        self._save_log.setEnabled(connected and idle)
        logging = connected and idle and self._save_log.isChecked()
        self._log_location.setEnabled(logging)
        self._browse.setEnabled(logging)

    def _set_log_location(self, location):
        self._log_location.setText(location)
        self._log_location.setToolTip(f"CSV files go into {location}/{config.LOG_FOLDER_NAME}")

    def _on_timer(self):
        if self.runtime is not None:
            self.runtime.poll()       # calls _on_sample / _log / _on_run_finished for new events
        self._live_plot.refresh()
