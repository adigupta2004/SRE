"""
GUI components (PySide6 + pyqtgraph). The GUI never talks to hardware: it only
receives domain-level data (samples, messages, run results) and sends requests
(start a profile, stop) to the control layer.

    trace_plot        reusable plot: named traces, legend, show/hide, autoscale, clear
    profile_editor    profile picker with fields built from each profile's PARAMS
    telemetry_panel   grid of live label/value pairs
    dyno_window       main window combining the above with a DynoRuntime
"""
