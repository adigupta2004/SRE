"""
Shared code for the regenerative dynamometer: used by every test script
(test_XX_*.py) and by the final application (dyno_app.py).

Modules, lowest layer first:
    config      user-editable settings (serial port, encoder, loop rate, limits)
    registers   Delta C2000 Modbus register map and command words (hardware facts)
    mathutils   pure math: word/counter arithmetic, unit conversion, filters
    timing      fixed-rate loop pacing
    console     terminal output helpers (banners, status line)
    comms       serial / Modbus RTU link setup
    c2000       C2000 driver: every read/write to the dyno VFD goes through here
    encoder     signed shaft RPM from the C2000's 32-bit encoder position
"""
