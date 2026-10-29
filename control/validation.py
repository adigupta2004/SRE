"""
Commanded vs estimated torque comparison, computed from a run log.

The scored estimate is estimated_torque_nm: the dyno's electromagnetic torque, derived
from the drive's output power minus the dyno motor's no-load losses (control/runtime.py),
i.e. the torque the drive controls (the coupling torque adds the dyno's own friction and
windage, which is not measured). It is not an
independent torque measurement (it relies on the drive's own voltage and current values
and on the loss table in config.py), but it reflects real torque, transients included.
The C2000's own estimate (2208H) is not scored: it is a motor-model value, ~17 % low,
that follows the command exactly (see C2000.Telemetry.torque_nm).

Works on numpy arrays (columns read from the CSV by control.run_log.read_log), so a
run of any length is compared without keeping per-sample Python objects in memory.
"""

from dataclasses import dataclass

import numpy as np

from control.runtime import RUNNING

ESTIMATE_COLUMN = "estimated_torque_nm"
# Run-log columns that running_torque_error() needs.
ERROR_COLUMNS = ("phase", "elapsed_time", "torque_command_nm", ESTIMATE_COLUMN)


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
    Arguments are equal-length arrays; returns (times, errors) arrays. Blank
    estimates (NaN, e.g. below MIN_RUN_RPM) give NaN errors.
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
                        columns[ESTIMATE_COLUMN][running])


def error_stats(errors):
    """Bias, mean absolute error and maximum absolute error over the non-blank errors; None if none."""
    errors = np.asarray(errors, dtype=float)
    errors = errors[np.isfinite(errors)]
    if errors.size == 0:
        return None
    return ErrorStats(
        bias=float(errors.mean()),
        mae=float(np.abs(errors).mean()),
        max_abs=float(np.abs(errors).max()),
        n=int(errors.size),
    )
