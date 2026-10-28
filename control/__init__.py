"""
Dyno control logic: no Modbus details (those live in hardware/) and no GUI code.

    torque_profiles   torque target as a function of profile time and shaft RPM
    runtime           the ~10 Hz read -> evaluate -> limit -> command -> log loop
    runtime_process   runs DynoRuntime in its own process; the GUI's handle to it
    run_log           one CSV file per run
    validation        commanded vs estimated torque error statistics
"""
