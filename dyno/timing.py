"""
Loop timing helpers.
"""

import time


class RatePacer:
    """
    Paces a loop at a nominal rate. If an iteration overruns, the schedule
    restarts from now instead of trying to catch up with a burst of iterations.
    Callers should still measure the actual dt from sample timestamps.
    """

    def __init__(self, rate_hz):
        self.period = 1.0 / rate_hz
        self.next_t = time.monotonic()

    def wait(self):
        self.next_t += self.period
        delay = self.next_t - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            self.next_t = time.monotonic()
