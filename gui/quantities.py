"""
The plottable quantities, shared by the live plot (DynoWindow) and the run-log
viewer (LogView).

Each quantity is a runtime Sample field, which is also the run-log CSV column of the
same name, so one definition serves both: the live plot takes the value from each
Sample, the viewer reads the column from the CSV. All quantities share one y-axis
(it autoscales to the ticked ones), so every label carries its unit.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Quantity:
    label: str            # checkbox text, with unit
    column: str           # Sample field = CSV column
    color: str            # dark, saturated: reads well as a 1-px line
    shown: bool = False   # ticked when the window opens


QUANTITIES = [
    Quantity("Torque command (N·m)", "torque_command_nm", "#08519c", shown=True),
    Quantity("C2000 torque estimate (N·m)", "torque_c2000_nm", "#b2182b", shown=True),
    Quantity("Elec. P/ω torque estimate (N·m)", "torque_elec_power_nm", "#1b7837", shown=True),
    Quantity("Encoder speed (rpm)", "encoder_rpm", "#54278f"),
    Quantity("Filtered speed (rpm)", "filtered_rpm", "#8073ac"),
    Quantity("C2000 speed (rpm)", "c2000_rpm", "#525252"),
    Quantity("Current (A)", "current_a", "#01665e"),
    Quantity("Output power (kW)", "output_power_kw", "#c51b7d"),
    Quantity("DC bus (V)", "dc_bus_v", "#000000"),
]
