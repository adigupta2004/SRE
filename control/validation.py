"""
Commanded vs C2000-estimated torque comparison.

The C2000 estimate (2208H) is the drive's own model output, not an independent
measurement: these numbers describe agreement between the command and the
drive's estimate, not absolute torque accuracy.
"""

from dataclasses import dataclass


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
    Returns (times, errors).
    """
    return times[1:], [est - cmd for est, cmd in zip(estimates[1:], commands[:-1])]


def error_stats(errors):
    """Bias, mean absolute error and maximum absolute error; None if there is no data."""
    if not errors:
        return None
    return ErrorStats(
        bias=sum(errors) / len(errors),
        mae=sum(abs(e) for e in errors) / len(errors),
        max_abs=max(abs(e) for e in errors),
        n=len(errors),
    )
