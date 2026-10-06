"""
Pure math helpers: no I/O, no hardware, safe to unit-test on any machine.
"""


def combine_words(low_word, high_word):
    """
    Combines two 16-bit registers into an unsigned 32-bit pattern.
    Delta transfers 32-bit values with the low word at the lower address.
    Both words must be unsigned 0..65535, as returned by read_registers().
    """
    return (high_word << 16) | low_word


def to_signed32(raw):
    """Two's-complement interpretation of an unsigned 32-bit pattern (for display)."""
    return raw - 2**32 if raw >= 2**31 else raw


def wrap_delta(new, old, modulus):
    """
    Signed shortest distance from old to new on a counter that wraps at modulus.
    Correct only while the counter moves less than modulus / 2 between readings.
    """
    forward = (new - old) % modulus      # distance going forward round the ring: 0 .. modulus-1
    if forward >= modulus // 2:          # more than half way round forward ->
        return forward - modulus         # the shorter path is backward (negative)
    return forward


def counts_to_rpm(d_counts, counts_per_rev, dt_s):
    """Converts a count change over dt_s seconds to revolutions per minute."""
    return (d_counts / counts_per_rev) / dt_s * 60.0


class LowPassFilter:
    """
    First-order low-pass with time constant tau, for irregular sample times:
    alpha = dt / (tau + dt). The first input initialises the output directly.
    """

    def __init__(self, tau_s):
        self.tau_s = tau_s
        self.value = None

    def update(self, x, dt_s):
        if self.value is None:
            self.value = x
        else:
            alpha = dt_s / (self.tau_s + dt_s)
            self.value += alpha * (x - self.value)
        return self.value
