"""
LogView: the run-log viewer - everything about results, shown as DynoWindow's
Results tab.

It shows a run log (a CSV written by control.run_log): a summary header, one plot
with a checkbox per quantity (gui/quantities.py, plus the torque error), and the
validation statistics, plus an "Open CSV…" button for any older log. All of it is
read back from the file rather than kept in memory during the run, so runs of any
length can be viewed: control.run_log.read_log reads the plotted columns as compact
numpy arrays (a 10-hour log: ~30 MB, ~2 s to open), and TracePlot downsamples long
traces for drawing. Every column is read up front, so ticking a box needs no re-read.

    show_run(result)  a run has just finished: its summary, then plots and statistics
                      from its CSV (summary only if it was not saved)
    open_log(path)    any saved log; "Open CSV…" calls this, with or without a drive

It is self-contained: it reads files in the GUI process and needs neither the control
process nor the drive. Problems (an unreadable file, columns missing from older logs)
are shown in its own summary.
"""

import os

from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

import config
from control.run_log import read_log
from control.validation import ERROR_COLUMNS, error_stats, running_torque_error
from gui.quantities import QUANTITIES
from gui.trace_plot import TracePlot

TIME_COLUMN = "elapsed_time"

# Only in the viewer: computed from the log (RUNNING rows), not a column of it.
ERROR_LABEL = "Torque error: estimated − command (N·m)"
ERROR_COLOR = "#e66101"


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

        self._plot = TracePlot("Run", "time (s)", "")
        for q in QUANTITIES:
            self._plot.add_trace(q.label, color=q.color, visible=q.shown)
        self._plot.add_trace(ERROR_LABEL, color=ERROR_COLOR, visible=True)
        # Every column any trace needs, read once per log.
        self._columns = sorted({TIME_COLUMN, *(q.column for q in QUANTITIES), *ERROR_COLUMNS})

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self._plot)

    # --------------------------------------------------------------------------
    # Public API
    # --------------------------------------------------------------------------
    def show_run(self, result):
        """Shows a just-finished run (a control.runtime.RunResult)."""
        if result.log_path:
            try:
                _, details = self._load(result.log_path)
            except (OSError, ValueError) as e:
                self._plot.clear()
                details = [f"Could not read the CSV: {e}"]
        else:
            self._plot.clear()
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
            self._plot.clear()                    # summary and plot always describe the same file
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

        self._plot.clear()
        missing = []                              # e.g. a log from before a column existed
        for q in QUANTITIES:
            if q.column in columns:
                self._plot.set_data(q.label, columns[TIME_COLUMN], columns[q.column])
            else:
                missing.append(q.label)
        has_error_columns = all(c in columns for c in ERROR_COLUMNS)
        if has_error_columns:
            self._plot.set_data(ERROR_LABEL, *running_torque_error(columns))
        else:
            missing.append(ERROR_LABEL)
        self._plot.refresh()
        details = [f"Not in this log: {', '.join(missing)}"] if missing else []
        stats = error_stats(running_torque_error(columns)[1]) if has_error_columns else None
        if not has_error_columns:
            details.append("Statistics not available (no estimated torque in this log).")
        elif stats is None:
            details.append("Statistics not available (no RUNNING-phase data).")
        else:
            details.append(f"<b>Bias (mean error):</b> {stats.bias:+.2f} N·m &nbsp; "
                           f"<b>Mean absolute error:</b> {stats.mae:.2f} N·m &nbsp; "
                           f"<b>Max absolute error:</b> {stats.max_abs:.2f} N·m &nbsp; ({stats.n} samples)")
        return comments, details
