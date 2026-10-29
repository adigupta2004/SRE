"""
TracePlot: a plot of any number of named traces sharing one x and one y axis,
with a show/hide checkbox per trace (each with a short line sample in the trace's
colour, so the checkboxes are also the legend) and autoscaling over the visible
traces. Data can be appended point by point (live) or set in one go (results); the
owner calls refresh() from a GUI timer so redraw cost is independent of the data rate.

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

cursor=True adds a hover cursor (used by the results viewer): moving the mouse over the
plot shows a vertical line at the nearest sample and, next to every trace's checkbox
(ticked or not), that trace's exact value there, plus the time. The line snaps to the
samples of the trace with the most points; a trace shows a value only if it has a sample
at that same time (traces of one run log share its time column) and the value is not
blank, otherwise "–". Lookups are binary searches on the x arrays (which must be
increasing), so the cursor stays instant on logs of many hours.
"""

from collections import deque

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QCheckBox, QGridLayout, QLabel, QVBoxLayout, QWidget

pg.setConfigOptions(background="w", foreground="k", antialias=True)

DEFAULT_COLORS = ["#08519c", "#b2182b", "#1b7837", "#e66101", "#54278f", "#8c510a"]
TOGGLES_PER_ROW = 5           # checkboxes wrap onto further rows
CURSOR_TOGGLES_PER_ROW = 3    # with cursor values next to the checkboxes, fewer fit in a row
                              # (3 keeps the results tab narrower than the live tab)
LINE_SAMPLE_SIZE = QSize(22, 10)
CURSOR_RATE_HZ = 30           # cursor updates per second at most while the mouse moves
NO_VALUE = "–"


def _line_sample(color, dashed):
    """A short, thick line in the trace's colour: the legend mark shown next to its checkbox."""
    pixmap = QPixmap(LINE_SAMPLE_SIZE)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor(color), 3, Qt.DashLine if dashed else Qt.SolidLine, Qt.FlatCap))
    y = LINE_SAMPLE_SIZE.height() / 2
    painter.drawLine(1, y, LINE_SAMPLE_SIZE.width() - 1, y)
    painter.end()
    return QIcon(pixmap)


class TracePlot(QWidget):
    def __init__(self, title, x_label, y_label, max_points=None, cursor=False, parent=None):
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

        self._curves = {}          # trace name -> pyqtgraph curve
        self._data = {}            # trace name -> (xs, ys): deques (append) or arrays (set_data)
        self._dirty = set()        # traces changed since the last refresh(), i.e. need a redraw
        self._markers = []         # labelled vertical lines (add_marker), removed by clear()

        # Hover cursor (cursor=True): a vertical line at the nearest sample, the time, and
        # every trace's value next to its checkbox.
        self._cursor = cursor
        self._x_label = x_label
        self._decimals = {}        # trace name -> decimals of its cursor value
        self._values = {}          # trace name -> QLabel with its cursor value
        if cursor:
            self._cursor_line = pg.InfiniteLine(
                angle=90, movable=False, pen=pg.mkPen("#7f7f7f", width=1, style=Qt.DashLine))
            self._cursor_line.setVisible(False)
            self._plot.addItem(self._cursor_line, ignoreBounds=True)    # never affects autoscale
            self._cursor_time = QLabel("")
            layout.addWidget(self._cursor_time)
            # Mouse moves arrive much faster than needed; SignalProxy passes on at most
            # CURSOR_RATE_HZ of them. Leaving the plot hides the cursor (eventFilter).
            self._mouse_proxy = pg.SignalProxy(self._plot.scene().sigMouseMoved,
                                               rateLimit=CURSOR_RATE_HZ, slot=self._on_mouse_moved)
            self._plot.viewport().installEventFilter(self)
        layout.addLayout(self._toggles)

    def add_trace(self, name, color=None, dashed=False, visible=True, decimals=2):
        """
        Adds a trace (empty) and its checkbox; visible sets whether it starts ticked,
        decimals how its cursor value is shown (cursor=True only).
        """
        index = len(self._curves)
        color = color or DEFAULT_COLORS[index % len(DEFAULT_COLORS)]
        pen = pg.mkPen(color, width=1, style=Qt.DashLine if dashed else Qt.SolidLine)
        self._curves[name] = self._plot.plot([], [], name=name, pen=pen, connect="finite")
        self._curves[name].setVisible(visible)
        self._data[name] = self._new_buffers()

        toggle = QCheckBox(name)              # text in the normal colour, for readability
        toggle.setIcon(_line_sample(color, dashed))
        toggle.setIconSize(LINE_SAMPLE_SIZE)
        toggle.setChecked(visible)
        toggle.toggled.connect(lambda on, n=name: self.set_trace_visible(n, on))
        if not self._cursor:
            self._toggles.addWidget(toggle, index // TOGGLES_PER_ROW, index % TOGGLES_PER_ROW)
            return
        # With the cursor each trace takes two grid cells: checkbox, then its value. The value
        # label has a fixed width so the legend does not shift as the numbers change.
        value = QLabel("")
        value.setFixedWidth(value.fontMetrics().horizontalAdvance("-00000.000"))
        row, column = index // CURSOR_TOGGLES_PER_ROW, 2 * (index % CURSOR_TOGGLES_PER_ROW)
        self._toggles.addWidget(toggle, row, column)
        self._toggles.addWidget(value, row, column + 1)
        self._values[name] = value
        self._decimals[name] = decimals

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

    def add_marker(self, x, label):
        """Adds a labelled vertical line at x (e.g. an event in a run); clear() removes it."""
        marker = pg.InfiniteLine(
            pos=x, angle=90, movable=False, pen=pg.mkPen("#4d4d4d", width=1, style=Qt.DotLine),
            label=label, labelOpts={"position": 0.97, "color": "#4d4d4d", "anchors": [(0, 0), (0, 0)]})
        self._plot.addItem(marker, ignoreBounds=True)     # never affects autoscale
        self._markers.append(marker)

    def clear(self):
        for name in self._curves:
            self._data[name] = self._new_buffers()
            self._curves[name].setData([], [])
        for marker in self._markers:
            self._plot.removeItem(marker)
        self._markers.clear()
        self._dirty.clear()
        self._plot.enableAutoRange()
        self._hide_cursor()

    def _new_buffers(self):
        """x and y buffers for append(); with max_points set, the oldest points drop out."""
        return deque(maxlen=self._max_points), deque(maxlen=self._max_points)

    # --------------------------------------------------------------------------
    # Hover cursor (cursor=True only)
    # --------------------------------------------------------------------------
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Leave:
            self._hide_cursor()
        return False                          # never swallow the event

    def _on_mouse_moved(self, args):
        # 1. Mouse position -> x in data coordinates; outside the plot area: no cursor.
        view_box = self._plot.getPlotItem().getViewBox()
        position = args[0]
        if not view_box.sceneBoundingRect().contains(position):
            self._hide_cursor()
            return
        x = view_box.mapSceneToView(position).x()

        # 2. Snap to the nearest sample of the trace with the most points.
        arrays = {name: (np.asarray(xs, dtype=float), np.asarray(ys, dtype=float))
                  for name, (xs, ys) in self._data.items()}
        ref_xs = max((xs for xs, _ in arrays.values()), key=len)
        if len(ref_xs) == 0 or not ref_xs[0] <= x <= ref_xs[-1]:
            self._hide_cursor()
            return
        t = ref_xs[_nearest_index(ref_xs, x)]

        # 3. Show the line, the time, and each trace's value at that time.
        self._cursor_line.setPos(t)
        self._cursor_line.setVisible(True)
        self._cursor_time.setText(f"{self._x_label}: {t:.2f}")
        for name, (xs, ys) in arrays.items():
            text = NO_VALUE
            if len(xs):
                i = _nearest_index(xs, t)
                if xs[i] == t and np.isfinite(ys[i]):
                    text = f"{ys[i]:.{self._decimals[name]}f}"
            self._values[name].setText(text)

    def _hide_cursor(self):
        if not self._cursor:
            return
        self._cursor_line.setVisible(False)
        self._cursor_time.setText("")
        for value in self._values.values():
            value.setText("")


def _nearest_index(xs, x):
    """Index of the element of the increasing array xs nearest to x (binary search)."""
    i = int(np.searchsorted(xs, x))
    if i == 0:
        return 0
    if i == len(xs):
        return len(xs) - 1
    return i if xs[i] - x < x - xs[i - 1] else i - 1
