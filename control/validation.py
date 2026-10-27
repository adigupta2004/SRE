"""
Commanded vs C2000-estimated torque comparison, computed from a run log.

The C2000 estimate (2208H) is the drive's own model output, not an independent
measurement: these numbers describe agreement between the command and the
drive's estimate, not absolute torque accuracy.

Works on numpy arrays (columns read from the CSV by control.run_log.read_log), so a
run of any length is compared without keeping per-sample Python objects in memory.
"""

from dataclasses import dataclass

import numpy as np

from control.runtime import RUNNING

# Run-log columns that running_torque_error() needs.
ERROR_COLUMNS = ("phase", "elapsed_time", "torque_command_nm", "torque_c2000_nm")


@dataclass(frozen=True)
class ErrorStats:
    bias: float         # mean error: systematic offset
    mae: float          # mean absolute error: typical error
    max_abs: float      # worst error
    n: int


def torque_error(times, commands, estimates):
    """
    Error = estimate - command, pairing each estimate with the command that was
    in force when it was read. Telemetry is read before each cycle's torque
    write, so that is the previous cycle's command; the first sample is dropped.
    Arguments are equal-length arrays; returns (times, errors) arrays.
    """
    times, commands, estimates = (np.asarray(a, dtype=float) for a in (times, commands, estimates))
    return times[1:], estimates[1:] - commands[:-1]


def running_torque_error(columns):
    """
    torque_error() over the RUNNING rows of a run log only: the entry and stop ramps
    are plotted but not scored. columns: {name: array} with ERROR_COLUMNS.
    """
    running = columns["phase"] == RUNNING
    return torque_error(columns["elapsed_time"][running],
                        columns["torque_command_nm"][running],
                        columns["torque_c2000_nm"][running])


def error_stats(errors):
    """Bias, mean absolute error and maximum absolute error; None if there is no data."""
    errors = np.asarray(errors, dtype=float)
    if errors.size == 0:
        return None
    return ErrorStats(
        bias=float(errors.mean()),
        mae=float(np.abs(errors).mean()),
        max_abs=float(np.abs(errors).max()),
        n=int(errors.size),
    )
