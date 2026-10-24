"""
TelemetryPanel: a titled grid of label / value pairs for live readouts.
The owner formats values; the panel only displays them.
"""

from PySide6.QtWidgets import QFormLayout, QGroupBox, QLabel


class TelemetryPanel(QGroupBox):
    def __init__(self, title, fields, parent=None):
        """fields: sequence of (key, label)."""
        super().__init__(title, parent)
        layout = QFormLayout(self)
        self._values = {}
        for key, label in fields:
            value = QLabel("-")
            value.setMinimumWidth(110)
            layout.addRow(label, value)
            self._values[key] = value

    def set_values(self, values):
        """values: {key: text}. Unknown keys are ignored."""
        for key, text in values.items():
            if key in self._values:
                self._values[key].setText(text)
