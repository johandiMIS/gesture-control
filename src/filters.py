"""Smoothing filters for interactive pointing.

Plain exponential smoothing forces a bad choice: light smoothing leaves jitter,
heavy smoothing adds lag. The One Euro filter (Casiez, Roussel & Vogel, CHI
2012) sidesteps it - its cutoff frequency rises with the speed of the signal, so
a hand held still is smoothed hard while a fast sweep is barely touched.
"""

import math


def _alpha(cutoff, dt):
    tau = 1.0 / (2.0 * math.pi * cutoff)
    return 1.0 / (1.0 + tau / dt)


class Ema:
    """Fixed-rate exponential average, for signals that only need to be stable
    rather than responsive (palm radius, which is a gain term - jitter in it
    would modulate the whole mapping)."""

    def __init__(self, alpha=0.2):
        self.alpha = alpha
        self.value = None

    def __call__(self, x):
        if self.value is None:
            self.value = x
        else:
            self.value = self.alpha * x + (1.0 - self.alpha) * self.value
        return self.value

    def reset(self):
        self.value = None


class _LowPass:
    def __init__(self):
        self.y = None

    def __call__(self, x, alpha):
        self.y = x if self.y is None else alpha * x + (1.0 - alpha) * self.y
        return self.y


class OneEuroFilter:
    """Speed-adaptive low-pass filter.

    `min_cutoff` sets how hard a *stationary* signal is smoothed - lower is
    calmer but adds lag when motion starts. `beta` sets how fast the filter gets
    out of the way as the signal speeds up - higher means less lag but more
    jitter while moving. Tune min_cutoff first with the hand held still, then
    beta while sweeping.
    """

    def __init__(self, min_cutoff=1.2, beta=0.35, d_cutoff=1.0):
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self._x = _LowPass()
        self._dx = _LowPass()
        self._t = None

    def __call__(self, x, t):
        dt = None if self._t is None else t - self._t
        self._t = t

        # First sample, or a stalled clock: seed and skip the derivative.
        if dt is None or dt <= 0.0 or self._x.y is None:
            return self._x(x, 1.0)

        dx = (x - self._x.y) / dt
        edx = self._dx(dx, _alpha(self.d_cutoff, dt))
        cutoff = self.min_cutoff + self.beta * abs(edx)
        return self._x(x, _alpha(cutoff, dt))

    @property
    def value(self):
        return self._x.y

    def reseed(self, x):
        """Jump the filter state to `x` with a zeroed derivative - used when the
        controller deliberately relocates the output (grab rewind, reset)."""
        self._x.y = x
        self._dx.y = 0.0
