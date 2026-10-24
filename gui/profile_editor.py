"""
ProfileEditor: choose a torque profile and edit its parameters. The input
fields are generated from each profile class's PARAMS, so new profiles appear
without GUI changes. Values typed for one profile are kept when switching.
"""

from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QStackedWidget, QVBoxLayout, QWidget)


class ProfileEditor(QGroupBox):
    def __init__(self, profile_classes, title="Torque profile", parent=None):
        super().__init__(title, parent)
        self._classes = list(profile_classes)
        self._fields = []                      # per profile: {param key: spin box}

        # One form per profile, all built up front and kept in a stack; the combo box
        # only switches which form is visible, so typed values survive switching.
        self._combo = QComboBox()
        self._stack = QStackedWidget()
        for cls in self._classes:
            self._combo.addItem(cls.NAME)
            form_widget = QWidget()
            form = QFormLayout(form_widget)
            fields = {}
            for p in cls.PARAMS:
                spin = QDoubleSpinBox()
                spin.setRange(p.minimum, p.maximum)
                spin.setDecimals(p.decimals)
                spin.setSingleStep(p.step)
                spin.setValue(p.default)
                form.addRow(p.label, spin)
                fields[p.key] = spin
            self._fields.append(fields)
            self._stack.addWidget(form_widget)
        self._combo.currentIndexChanged.connect(self._stack.setCurrentIndex)

        layout = QVBoxLayout(self)
        layout.addWidget(self._combo)
        layout.addWidget(self._stack)

    def profile(self):
        """Returns an instance of the selected profile (raises ValueError if invalid)."""
        i = self._combo.currentIndex()
        return self._classes[i](**{key: spin.value() for key, spin in self._fields[i].items()})
