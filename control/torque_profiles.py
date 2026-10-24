"""
Torque profiles: the torque target (% of Pr.11-27, signed like Pr.11-34) as a
function of profile time and shaft speed.

Every profile implements:
    evaluate(t, rpm) -> target torque %    t = seconds since the profile started
    duration_s       -> seconds, or None to run until Stop

and declares its parameters in PARAMS, so a GUI can build input fields for any
profile without knowing what it does. To add a profile, subclass TorqueProfile,
fill in NAME / PARAMS / evaluate / duration_s, and append it to PROFILES.

Profiles return the raw target; the runtime applies the torque limits.
"""

from dataclasses import dataclass

import config


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    default: float
    minimum: float
    maximum: float
    step: float = 0.1
    decimals: int = 1


def torque_param(key, label, default):
    """A torque parameter, bounded by the commissioning torque limits."""
    return Param(key, label, default, config.TORQUE_LIMIT_MIN_PCT, config.TORQUE_LIMIT_MAX_PCT)


def time_param(key, label, default, minimum=0.0):
    return Param(key, label, default, minimum, 3600.0, step=1.0)


class TorqueProfile:
    NAME = ""
    PARAMS = []

    def __init__(self, **values):
        for p in self.PARAMS:
            value = float(values.get(p.key, p.default))
            if not p.minimum <= value <= p.maximum:
                raise ValueError(f"{p.label} must be between {p.minimum:g} and {p.maximum:g}.")
            setattr(self, p.key, value)

    @property
    def duration_s(self):
        return None

    def evaluate(self, t, rpm):
        raise NotImplementedError

    def describe(self):
        """One line identifying the profile and its parameters (used in logs)."""
        params = ", ".join(f"{p.key}={getattr(self, p.key):g}" for p in self.PARAMS)
        return f"{self.NAME} ({params})"


class ConstantTorque(TorqueProfile):
    NAME = "Constant torque"
    PARAMS = [
        torque_param("torque_pct", "Torque (%)", -2.0),
        time_param("run_s", "Duration (s, 0 = until Stop)", 30.0),
    ]

    @property
    def duration_s(self):
        return self.run_s or None       # run_s = 0 -> None -> run until Stop

    def evaluate(self, t, rpm):
        return self.torque_pct


class RampTorque(TorqueProfile):
    NAME = "Ramp"
    PARAMS = [
        torque_param("start_pct", "Start torque (%)", 0.0),
        torque_param("end_pct", "End torque (%)", -5.0),
        time_param("ramp_s", "Ramp time (s)", 20.0, minimum=1.0),
        time_param("hold_s", "Hold at end (s)", 10.0),
    ]

    @property
    def duration_s(self):
        return self.ramp_s + self.hold_s

    def evaluate(self, t, rpm):
        frac = min(t / self.ramp_s, 1.0)
        return self.start_pct + (self.end_pct - self.start_pct) * frac


class StepTorque(TorqueProfile):
    NAME = "Step"
    PARAMS = [
        torque_param("before_pct", "Before step (%)", -2.0),
        torque_param("after_pct", "After step (%)", -5.0),
        time_param("step_at_s", "Step at (s)", 10.0),
        time_param("run_s", "Total duration (s)", 30.0, minimum=1.0),
    ]

    @property
    def duration_s(self):
        return self.run_s

    def evaluate(self, t, rpm):
        return self.before_pct if t < self.step_at_s else self.after_pct


class LinearRpmTorque(TorqueProfile):
    NAME = "Linear vs RPM"
    PARAMS = [
        Param("slope_pct_per_rpm", "Slope m (%/rpm)", -0.002, -0.05, 0.05, step=0.0005, decimals=4),
        Param("intercept_pct", "Intercept c (%)", 0.0, -100.0, 100.0),
        time_param("run_s", "Duration (s, 0 = until Stop)", 30.0),
    ]

    @property
    def duration_s(self):
        return self.run_s or None

    def evaluate(self, t, rpm):
        # Signed RPM: with a negative slope, opposing torque grows with forward speed.
        return self.slope_pct_per_rpm * rpm + self.intercept_pct


PROFILES = [ConstantTorque, RampTorque, StepTorque, LinearRpmTorque]
