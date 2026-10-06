"""
Signed mechanical shaft RPM from the C2000's 32-bit motor actual position
(222CH/222DH), read via C2000.read_encoder_sample().

Interpretation of the 32-bit position:
  The combined value is a free-running two's-complement counter that wraps
  modulo 2^32. We keep the UNSIGNED bit pattern (0 .. 2^32-1) for arithmetic and
  only convert to signed for display. Speed uses the signed modulo-2^32
  difference between consecutive samples. This is equivalent to unsigned
  modulo-2^32 difference - just that the rollover boundary in unsigned is at 0,
  while in signed it is at the edge between +ve and -ve.
  Note - doing modulo is what handles rollover in either direction properly, in
  signed as well as unsigned. However, there is an assumption - this is correct
  only as long as the shaft moves less than half the counter size between samples.
  In this case, that limit becomes 2^31 counts between samples (2^31/4096 revolutions
  in 0.1s, which is physically impossible here). Reason - think of it in terms of an
  unsigned counter, as that is easier to visualise - two readings can always be
  explained by more than one movement on the circle. Take the bigger arc, smaller arc
  or integer revolutions plus some arc. We assume the smallest movement - ie the
  smaller arc. That represents reality only when the motion was less than half the
  circumference.

FWD (A leads B, Pr.10-02 = 1) = positive RPM; REV = negative RPM.
"""

from dataclasses import dataclass

from dyno import config
from dyno.mathutils import LowPassFilter, counts_to_rpm, wrap_delta

POSITION_MODULUS = 2**32


@dataclass(frozen=True)
class SpeedReading:
    dt: float                  # s since the previous sample
    d_counts: int              # signed position change since the previous sample
    rpm: float                 # raw encoder RPM
    rpm_filtered: float        # low-passed RPM (last good value if this sample was implausible)
    plausible: bool            # False -> register jump; rpm is not a real speed


class ShaftSpeedEstimator:
    """
    Turns successive EncoderSamples into signed shaft RPM.

    On a communication failure simply skip update(): the 32-bit delta over the
    longer, measured dt is still correct once communication recovers.
    Implausible samples are reported (plausible=False), excluded from the
    filter, and become the new reference for the next sample.
    """

    def __init__(
        self,
        first_sample,
        counts_per_rev=config.POSITION_COUNTS_PER_REV,
        filter_tau_s=config.SPEED_FILTER_TAU_S,
        max_plausible_rpm=config.MAX_PLAUSIBLE_RPM,
    ):
        self.prev = first_sample
        self.counts_per_rev = counts_per_rev
        self.max_plausible_rpm = max_plausible_rpm
        self.filter = LowPassFilter(filter_tau_s)

    def update(self, sample):
        dt = sample.t - self.prev.t
        d_counts = wrap_delta(sample.pos_raw, self.prev.pos_raw, POSITION_MODULUS)
        rpm = counts_to_rpm(d_counts, self.counts_per_rev, dt)
        plausible = abs(rpm) <= self.max_plausible_rpm
        if plausible:
            self.filter.update(rpm, dt)
        self.prev = sample
        return SpeedReading(dt, d_counts, rpm, self.filter.value, plausible)
