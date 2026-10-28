"""
CSV run log: one timestamped file per run, in <location>/LOG_FOLDER_NAME (created if
needed). The location is chosen by the user; it defaults to config.DEFAULT_LOG_LOCATION.

Two '#' lines identify the run (start time, profile); then one header row and one
row per runtime cycle. Column names match the runtime Sample fields; torque in N·m
(signed), rpm signed (+ = forward), power in kW (negative = regenerating), times in s.

This module owns the file format: RunLog writes it, read_log() reads it back.
"""

import csv
import datetime
import os
import re
from array import array

import numpy as np

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
    "torque_c2000_nm",       # 2208H converted with the motor rated torque: drive's motor-model value,
                             # not a usable torque reading yet (see C2000.Telemetry.torque_nm)
    "estimated_torque_nm",   # (power_vi - no-load losses) / ω: estimated shaft torque, scored by the
                             # error statistics; blank below MIN_RUN_RPM
    "torque_elec_nm",        # power_vi / ω: electrical torque, after the dyno motor's losses;
                             # blank below MIN_RUN_RPM; not scored
    "output_current_a",      # output current (AC)
    "output_voltage_v",      # output voltage (AC line-line)
    "power_angle_deg",       # signed angle between output voltage and current (+ motoring, - regen)
    "power_vi_kw",           # output power sqrt(3)·V·I·cos(angle) (negative = regenerating)
    "dc_bus_v",
]
TEXT_COLUMNS = {"phase"}     # every other column is numeric


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


def read_log(path, names):
    """
    Reads a run log written by RunLog. Returns (comments, columns):
        comments  the '#' lines at the top, without the '#'
        columns   {name: numpy array} for each of `names` present in the file
                  (absent names are left out, e.g. in logs from older versions);
                  numeric columns as float arrays, blank cells -> NaN; text columns
                  (TEXT_COLUMNS) as object arrays of shared strings.
    Only the requested columns are kept, at ~8 bytes per value, so even a run of
    many hours stays small in memory. Raises OSError / ValueError for unreadable files
    and for files that are not run logs.
    """
    comments = []
    with open(path, newline="") as f:
        for line in f:                               # '#' lines first, then the header row
            if not line.startswith("#"):
                header = next(csv.reader([line]))
                break
            comments.append(line[1:].strip())
        else:
            raise ValueError(f"{os.path.basename(path)} has no column header")
        if COLUMNS[0] not in header:                 # every run log, old or new, has elapsed_time
            raise ValueError(f"{os.path.basename(path)} is not a run log (no {COLUMNS[0]} column)")

        wanted = [(name, header.index(name)) for name in names if name in header]
        buffers = {name: [] if name in TEXT_COLUMNS else array("d") for name, _ in wanted}
        shared = {}                                  # one str object per distinct text value
        for row in csv.reader(f):
            for name, i in wanted:
                cell = row[i] if i < len(row) else ""
                if name in TEXT_COLUMNS:
                    buffers[name].append(shared.setdefault(cell, cell))
                else:
                    buffers[name].append(float(cell) if cell else np.nan)

    columns = {name: np.array(buf, dtype=object) if name in TEXT_COLUMNS else np.frombuffer(buf)
               for name, buf in buffers.items()}
    return comments, columns
