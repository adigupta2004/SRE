"""
Torque profiles: the torque target (N·m, signed like Pr.11-34: negative opposes
the forward-running MUT) as a function of profile time and shaft speed.

Every profile implements:
    evaluate(t, rpm) -> target torque N·m  t = seconds since the profile started
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
    """A torque parameter (N·m), bounded by the commissioning torque limits."""
    return Param(key, label, default, config.TORQUE_LIMIT_MIN_NM, config.TORQUE_LIMIT_MAX_NM, decimals=2)


def time_param(key, label, default, minimum=0.0):
    return Param(key, label, default, minimum, 3600.0, step=1.0)


class TorqueProfile:
    NAME = ""
    PARAMS = []

    def __init__(self, **values):
        for p in self.PARAMS:
            value = float(values.get(p.key, p.default))
            if not p.minimum <= value <= p.maximum:
                raise ValueError(f"{p.label} must be between {p.minimum:.{p.decimals}f} "
                                 f"and {p.maximum:.{p.decimals}f}.")
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
        torque_param("torque_nm", "Torque (N·m)", -2.0),
        time_param("run_s", "Duration (s, 0 = until Stop)", 30.0),
    ]

    @property
    def duration_s(self):
        return self.run_s or None       # run_s = 0 -> None -> run until Stop

    def evaluate(self, t, rpm):
        return self.torque_nm


class RampTorque(TorqueProfile):
    NAME = "Ramp"
    PARAMS = [
        torque_param("start_nm", "Start torque (N·m)", 0.0),
        torque_param("end_nm", "End torque (N·m)", -5.0),
        time_param("ramp_s", "Ramp time (s)", 20.0, minimum=1.0),
        time_param("hold_s", "Hold at end (s)", 10.0),
    ]

    @property
    def duration_s(self):
        return self.ramp_s + self.hold_s

    def evaluate(self, t, rpm):
        frac = min(t / self.ramp_s, 1.0)
        return self.start_nm + (self.end_nm - self.start_nm) * frac


class StepTorque(TorqueProfile):
    NAME = "Step"
    PARAMS = [
        torque_param("before_nm", "Before step (N·m)", -2.0),
        torque_param("after_nm", "After step (N·m)", -5.0),
        time_param("step_at_s", "Step at (s)", 10.0),
        time_param("run_s", "Total duration (s)", 30.0, minimum=1.0),
    ]

    @property
    def duration_s(self):
        return self.run_s

    def evaluate(self, t, rpm):
        return self.before_nm if t < self.step_at_s else self.after_nm


class LinearRpmTorque(TorqueProfile):
    NAME = "Linear vs RPM"
    PARAMS = [
        Param("slope_nm_per_rpm", "Slope m (N·m/rpm)", -0.002, -0.05, 0.05, step=0.0005, decimals=4),
        Param("intercept_nm", "Intercept c (N·m)", 0.0, -config.TORQUE_REFERENCE_NM, config.TORQUE_REFERENCE_NM,
              decimals=2),
        time_param("run_s", "Duration (s, 0 = until Stop)", 30.0),
    ]

    @property
    def duration_s(self):
        return self.run_s or None

    def evaluate(self, t, rpm):
        # Signed RPM: with a negative slope, opposing torque grows with forward speed.
        return self.slope_nm_per_rpm * rpm + self.intercept_nm


PROFILES = [ConstantTorque, RampTorque, StepTorque, LinearRpmTorque]
