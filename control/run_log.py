"""
CSV run log: one timestamped file per run, in <location>/LOG_FOLDER_NAME (created if
needed). The location is chosen by the user; it defaults to config.DEFAULT_LOG_LOCATION.

Two '#' lines identify the run (start time, profile); then one header row and one
row per runtime cycle. Column names match the runtime Sample fields; torque in N·m
(signed), rpm signed (+ = forward), power in kW (negative = regenerating), times in s.
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
    "torque_c2000_nm",       # 2208H converted with the motor rated torque (drive's own estimate)
    "torque_elec_power_nm",  # output power / filtered rpm (2206H, 0.1 kW steps): includes the dyno
                             # motor's losses, so offset by about +losses/ω vs the shaft torque;
                             # blank below MIN_RUN_RPM; not used in the error statistics
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
    def __init__(self, profile, location=config.DEFAULT_LOG_LOCATION):
        directory = os.path.join(location, config.LOG_FOLDER_NAME)
        os.makedirs(directory, exist_ok=True)
        now = datetime.datetime.now()
        slug = re.sub(r"\W+", "_", profile.NAME).strip("_").lower()
        self.path = os.path.join(directory, f"run_{now:%Y%m%d_%H%M%S}_{slug}.csv")
        self._file = open(self.path, "w", newline="")
        self._file.write(f"# Dyno run {now:%Y-%m-%d %H:%M:%S}\n")
        self._file.write(f"# Profile: {profile.describe()}\n")
        self._writer = csv.writer(self._file)
        self._writer.writerow(COLUMNS)

    def write(self, sample):
        self._writer.writerow(_cell(getattr(sample, c)) for c in COLUMNS)

    def close(self):
        self._file.close()
