"""
LogView: the run-log viewer - everything about results, shown as DynoWindow's
Results tab.

It shows a run log (a CSV written by control.run_log): a summary header, the torque
comparison and torque-error plots, and the validation statistics, plus an "Open CSV…"
button for any older log. All of it is read back from the file rather than kept in
memory during the run, so runs of any length can be viewed: control.run_log.read_log
reads only the columns needed, as compact numpy arrays (a 10-hour log: ~15 MB, ~1.3 s
to open), and TracePlot downsamples long traces for drawing.

    show_run(result)  a run has just finished: its summary, then plots and statistics
                      from its CSV (summary only if it was not saved)
    open_log(path)    any saved log; "Open CSV…" calls this, with or without a drive

It is self-contained: it reads files in the GUI process and needs neither the control
process nor the drive. Problems (an unreadable file, columns missing from older logs)
are shown in its own summary.
"""

import os
from dataclasses import dataclass

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

import config
from control.run_log import read_log
from control.validation import ERROR_COLUMNS, error_stats, running_torque_error
from gui.trace_plot import TracePlot

TIME_COLUMN = "elapsed_time"

# Torque from the drive's electrical output power / shaft speed (see Sample.torque_elec_power_nm).
# Shared with DynoWindow's live plot, so both show the same name.
TORQUE_ELEC_POWER_TRACE = "Elec. P/ω estimate"


@dataclass(frozen=True)
class _Trace:
    label: str
    column: str = None            # a CSV column, plotted against elapsed time, or ...
    compute: object = None        # ... compute(columns) -> (x, y) from the loaded columns
    uses: tuple = ()              # the columns compute() needs
    color: str = None


# The plots, top to bottom: (title, y-axis label, traces). To show another quantity,
# add a panel or a trace here; load() reads whatever columns these need.
PANELS = [
    ("Torque comparison", "torque (N·m)", [
        _Trace("Command", column="torque_command_nm"),
        _Trace("C2000 estimate", column="torque_c2000_nm"),
        _Trace(TORQUE_ELEC_POWER_TRACE, column="torque_elec_power_nm"),
    ]),
    ("Torque error (C2000 estimate - command)", "error (N·m)", [
        _Trace("Error", compute=running_torque_error, uses=ERROR_COLUMNS, color="#d62728"),
    ]),
]


class LogView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        # "Open CSV…" starts in the folder of the last log shown.
        self._folder = os.path.join(config.DEFAULT_LOG_LOCATION, config.LOG_FOLDER_NAME)

        self._summary = QLabel("No run yet.")
        self._summary.setWordWrap(True)
        open_button = QPushButton("Open CSV…")
        open_button.clicked.connect(self._on_open)
        header = QHBoxLayout()
        header.addWidget(self._summary, stretch=1)
        header.addWidget(open_button)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        self._plots = []                          # (TracePlot, its traces)
        needed = {TIME_COLUMN}                    # every column any trace needs
        for title, y_label, traces in PANELS:
            plot = TracePlot(title, "time (s)", y_label)
            for t in traces:
                plot.add_trace(t.label, color=t.color)
                needed.update([t.column] if t.column else t.uses)
            layout.addWidget(plot)
            self._plots.append((plot, traces))
        self._columns = sorted(needed)

    # --------------------------------------------------------------------------
    # Public API
    # --------------------------------------------------------------------------
    def show_run(self, result):
        """Shows a just-finished run (a control.runtime.RunResult)."""
        if result.log_path:
            try:
                _, details = self._load(result.log_path)
            except (OSError, ValueError) as e:
                self._clear_plots()
                details = [f"Could not read the CSV: {e}"]
        else:
            self._clear_plots()
            details = ["Not saved: no plots or statistics."]
        loop_hz = 1.0 / result.mean_loop_dt if result.mean_loop_dt else 0.0
        self._summary.setText(
            f"<b>{result.profile}</b><br>"
            f"Stopped: {result.reason} | Duration {result.duration_s:.1f} s | "
            f"Mean loop rate {loop_hz:.1f} Hz | Overruns {result.overruns}/{result.cycles} cycles<br>"
            f"CSV: {result.log_path or 'not saved'}<br>" + "<br>".join(details)
        )

    def open_log(self, path):
        """Shows any saved run log."""
        try:
            comments, details = self._load(path)
        except (OSError, ValueError) as e:
            self._clear_plots()                   # summary and plots always describe the same file
            self._summary.setText(f"<b>Could not read {os.path.basename(path)}</b><br>{e}")
            return
        # The first two '#' lines are the start time and the profile (older logs have more).
        self._summary.setText(f"<b>{os.path.basename(path)}</b><br>"
                              + "<br>".join(comments[:2] + details))

    # --------------------------------------------------------------------------
    # Internals
    # --------------------------------------------------------------------------
    def _on_open(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open run log", self._folder, "CSV files (*.csv)")
        if path:                                  # empty if the dialog was cancelled
            self.open_log(path)

    def _load(self, path):
        """
        Reads the log at path, plots every trace it can provide and computes the
        statistics. Returns (comments, details): the log's '#' lines, and summary lines
        about its contents (columns it lacks, the statistics). Raises OSError /
        ValueError if the file cannot be read.
        """
        comments, columns = read_log(path, self._columns)
        self._folder = os.path.dirname(path)

        missing = []
        for plot, traces in self._plots:
            plot.clear()
            for t in traces:
                if t.column and t.column in columns and TIME_COLUMN in columns:
                    plot.set_data(t.label, columns[TIME_COLUMN], columns[t.column])
                elif t.compute and all(c in columns for c in t.uses):
                    plot.set_data(t.label, *t.compute(columns))
                else:
                    missing.append(t.label)       # e.g. a log from before that column existed
            plot.refresh()
        details = [f"Not in this log: {', '.join(missing)}"] if missing else []
        stats = (error_stats(running_torque_error(columns)[1])
                 if all(c in columns for c in ERROR_COLUMNS) else None)
        if stats is None:
            details.append("Statistics not available (no RUNNING-phase data).")
        else:
            details.append(f"<b>Bias (mean error):</b> {stats.bias:+.2f} N·m &nbsp; "
                           f"<b>Mean absolute error:</b> {stats.mae:.2f} N·m &nbsp; "
                           f"<b>Max absolute error:</b> {stats.max_abs:.2f} N·m &nbsp; ({stats.n} samples)")
        return comments, details

    def _clear_plots(self):
        for plot, _ in self._plots:
            plot.clear()
