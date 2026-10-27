"""
TracePlot: a plot of any number of named traces sharing one x and one y axis,
with a legend, a show/hide checkbox per trace and autoscaling over the visible
traces. Data can be appended point by point (live) or set in one go (results);
the owner calls refresh() from a GUI timer so redraw cost is independent of the
data rate.

Live plots pass max_points: each trace then keeps only its newest max_points
points (a rolling window) and is drawn with thin, non-antialiased lines. Every
refresh redraws a whole trace, so without this the cost of a redraw - and the time
the GUI spends on it - would grow for as long as a run lasts. Results plots leave
max_points unset: they keep all their data and are drawn once.
"""

from collections import deque

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QVBoxLayout, QWidget

pg.setConfigOptions(background="w", foreground="k", antialias=True)

DEFAULT_COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#ff7f0e", "#9467bd", "#8c564b"]


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
        self._plot.addLegend(offset=(10, 10))

        self._toggles = QHBoxLayout()
        self._toggles.addStretch()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._plot)
        layout.addLayout(self._toggles)

        self._curves = {}          # trace name -> pyqtgraph curve
        self._data = {}            # trace name -> (xs, ys) deques; append() only adds here
        self._dirty = set()        # traces changed since the last refresh(), i.e. need a redraw

    def add_trace(self, name, color=None, dashed=False):
        color = color or DEFAULT_COLORS[len(self._curves) % len(DEFAULT_COLORS)]
        live = self._max_points is not None
        pen = pg.mkPen(color, width=1 if live else 2, style=Qt.DashLine if dashed else Qt.SolidLine)
        self._curves[name] = self._plot.plot([], [], name=name, pen=pen, antialias=not live)
        self._data[name] = self._new_buffers()

        toggle = QCheckBox(name)
        toggle.setChecked(True)
        toggle.toggled.connect(lambda on, n=name: self.set_trace_visible(n, on))
        self._toggles.insertWidget(self._toggles.count() - 1, toggle)

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
        self._data[name] = self._new_buffers(xs, ys)
        self._dirty.add(name)

    def refresh(self):
        for name in self._dirty:
            xs, ys = self._data[name]
            self._curves[name].setData(list(xs), list(ys))
        self._dirty.clear()

    def clear(self):
        for name in self._curves:
            self._data[name] = self._new_buffers()
            self._curves[name].setData([], [])
        self._dirty.clear()
        self._plot.enableAutoRange()

    def _new_buffers(self, xs=(), ys=()):
        """x and y buffers; with max_points set, the oldest points drop out automatically."""
        return deque(xs, maxlen=self._max_points), deque(ys, maxlen=self._max_points)
