"""
TracePlot: a plot of any number of named traces sharing one x and one y axis,
with a show/hide checkbox per trace (written in the trace's colour, so the
checkboxes are also the legend) and autoscaling over the visible traces. Data can be appended point by point (live) or set in one go (results);
the owner calls refresh() from a GUI timer so redraw cost is independent of the
data rate.

Live plots pass max_points: each trace then keeps only its newest max_points
points (a rolling window). Every refresh redraws a whole trace, so without this the
cost of a redraw - and the time the GUI spends on it - would grow for as long as a
run lasts.

Without max_points the plot shows whole data sets given with set_data() (e.g. a run
log of many hours): the arrays are kept as given, not copied, and pyqtgraph draws a
downsampled version of only the visible range, so even very long traces stay fast.

All traces are drawn with 1-px antialiased lines: Qt draws wide antialiased lines
about 200x slower (a 10-hour trace: ~4 s per repaint at 2 px, ~0.02 s at 1 px).
Blank values (NaN) break a trace instead of being drawn.
"""

from collections import deque

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QGridLayout, QVBoxLayout, QWidget

pg.setConfigOptions(background="w", foreground="k", antialias=True)

DEFAULT_COLORS = ["#08519c", "#b2182b", "#1b7837", "#e66101", "#54278f", "#8c510a"]
TOGGLES_PER_ROW = 5           # checkboxes wrap onto further rows


class TracePlot(QWidget):
    def __init__(self, title, x_label, y_label, max_points=None, parent=None):
        super().__init__(parent)
        self._max_points = max_points       # None = keep everything
        self._plot = pg.PlotWidget(title=title)
        self._plot.setLabel("bottom", x_label)
        self._plot.setLabel("left", y_label)
        for axis in ("bottom", "left"):
            self._plot.getAxis(axis).enableAutoSIPrefix(False)    # no "(x0.001)" rescaling
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        if max_points is None:
            self._plot.setDownsampling(auto=True, mode="peak")    # keeps spikes visible
            self._plot.setClipToView(True)

        self._toggles = QGridLayout()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._plot)
        layout.addLayout(self._toggles)

        self._curves = {}          # trace name -> pyqtgraph curve
        self._data = {}            # trace name -> (xs, ys): deques (append) or arrays (set_data)
        self._dirty = set()        # traces changed since the last refresh(), i.e. need a redraw

    def add_trace(self, name, color=None, dashed=False, visible=True):
        """Adds a trace (empty) and its checkbox; visible sets whether it starts ticked."""
        index = len(self._curves)
        color = color or DEFAULT_COLORS[index % len(DEFAULT_COLORS)]
        pen = pg.mkPen(color, width=1, style=Qt.DashLine if dashed else Qt.SolidLine)
        self._curves[name] = self._plot.plot([], [], name=name, pen=pen, connect="finite")
        self._curves[name].setVisible(visible)
        self._data[name] = self._new_buffers()

        toggle = QCheckBox(name)
        toggle.setStyleSheet(f"color: {color}")
        toggle.setChecked(visible)
        toggle.toggled.connect(lambda on, n=name: self.set_trace_visible(n, on))
        self._toggles.addWidget(toggle, index // TOGGLES_PER_ROW, index % TOGGLES_PER_ROW)

    def set_trace_visible(self, name, visible):
        self._curves[name].setVisible(visible)
        self._plot.enableAutoRange()        # re-fit to the traces still shown

    def append(self, name, x, y):
        """Adds one point; None values are skipped (no data this cycle)."""
        if x is None or y is None:
            return
        xs, ys = self._data[name]
        xs.append(x)
        ys.append(y)
        self._dirty.add(name)

    def set_data(self, name, xs, ys):
        """Replaces a trace's data in one go; arrays are kept as given (no copy)."""
        self._data[name] = (xs, ys)
        self._dirty.add(name)

    def refresh(self):
        for name in self._dirty:
            xs, ys = self._data[name]
            self._curves[name].setData(np.asarray(xs, dtype=float), np.asarray(ys, dtype=float))
        self._dirty.clear()

    def clear(self):
        for name in self._curves:
            self._data[name] = self._new_buffers()
            self._curves[name].setData([], [])
        self._dirty.clear()
        self._plot.enableAutoRange()

    def _new_buffers(self):
        """x and y buffers for append(); with max_points set, the oldest points drop out."""
        return deque(maxlen=self._max_points), deque(maxlen=self._max_points)
