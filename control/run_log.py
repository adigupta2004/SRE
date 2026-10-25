"""
CSV run log: one timestamped file per run.

Lines starting with '#' record the test conditions; then one header row and one
row per runtime cycle. Column names match the runtime Sample fields.
"""

import csv
import datetime
import os
import re

import config

COLUMNS = [
    "elapsed_time",          # s since RUN
    "loop_dt",               # s between this cycle's start and the previous one's
    "cycle_time",            # s spent doing this cycle's work (> period = overrun)
    "phase",                 # ENTERING / RUNNING / STOPPING
    "profile_time",          # s since the profile started (blank outside RUNNING)
    "profile_target_nm",     # raw profile target before limits (blank outside ENTERING/RUNNING)
    "torque_command_nm",     # torque in force after this cycle's write (last accepted, after 0.1 % rounding)
    "limited",               # 1 if the target was clamped to the torque limits
    "encoder_rpm",
    "filtered_rpm",
    "c2000_rpm",
    "torque_estimate_nm",    # 2208H converted with the motor rated torque
    "current_a",
    "output_power_kw",
    "dc_bus_v",
]


def _cell(value):
    """CSV cell text: None -> blank (not applicable this cycle), bool -> 0/1, floats to 4 decimals."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return round(value, 4)
    return value


class RunLog:
    def __init__(self, profile, directory=config.LOG_DIR):
        os.makedirs(directory, exist_ok=True)
        now = datetime.datetime.now()
        slug = re.sub(r"\W+", "_", profile.NAME).strip("_").lower()
        self.path = os.path.join(directory, f"run_{now:%Y%m%d_%H%M%S}_{slug}.csv")
        self._file = open(self.path, "w", newline="")
        for line in (
            f"Dyno run log - started {now:%Y-%m-%d %H:%M:%S}",
            f"Profile: {profile.describe()}",
            f"Torque limits: {config.TORQUE_LIMIT_MIN_NM:.2f} .. {config.TORQUE_LIMIT_MAX_NM:.2f} Nm "
            f"| Max |rpm|: {config.MAX_ABS_RPM:g} | Min run rpm: {config.MIN_RUN_RPM:g} "
            f"| Slew: {config.TORQUE_SLEW_NM_PER_S:g} Nm/s | Loop: {config.LOOP_RATE_HZ:g} Hz",
            f"Torque scaling: Pr.11-27 = {config.PR_11_27_PCT:g} % "
            f"| command 100 % (Pr.11-34) = {config.TORQUE_REFERENCE_NM:.2f} Nm "
            f"| estimate 100 % (2208H) = motor rated torque = {config.MOTOR_RATED_TORQUE_NM:.2f} Nm",
            "Units: time s; torque Nm (signed); rpm signed (+ = forward); "
            "power kW (negative = regenerating)",
        ):
            self._file.write(f"# {line}\n")
        self._writer = csv.writer(self._file)
        self._writer.writerow(COLUMNS)

    def write(self, sample):
        self._writer.writerow(_cell(getattr(sample, c)) for c in COLUMNS)

    def close(self):
        self._file.close()
