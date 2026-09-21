"""Audio-driven motion: relative transients, not loudness pinned at a ceiling."""
import math


class AudioMotion:
    def __init__(self):
        self.mean = [0.0] * 3
        self.previous = [0.0] * 3
        self.envelope = [0.0] * 3

    def update(self, levels, dt):
        dt = min(.1, max(.001, dt))
        result = []
        for i, value in enumerate(levels):
            raw = float(value)
            raw = min(1.0, max(0.0, raw)) if math.isfinite(raw) else 0.0
            baseline = self.mean[i]
            # Relative deviation preserves accents in both quiet and mastered tracks.
            relative = max(0.0, raw - baseline) / max(.008, baseline, raw * .25)
            change = abs(raw - self.previous[i]) / max(.008, baseline, raw * .25)
            transient = min(.9, 1.3 * relative + 2.0 * change)
            self.mean[i] += (raw - baseline) * (1 - math.exp(-dt / .38))
            self.previous[i] = raw
            decay = (.13, .095, .065)[i]
            self.envelope[i] = max(transient, self.envelope[i] * math.exp(-dt / decay))
            # A steady loud signal settles, rather than permanently inflating glass.
            result.append(min(.92, self.envelope[i] + .035 * math.sqrt(raw)))
        return tuple(result)
