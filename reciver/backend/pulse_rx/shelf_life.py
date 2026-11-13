"""Remaining-shelf-life prediction from the 6-hourly temperature series of each box.

Physics: quality loss rate follows a Q10 / Arrhenius-style law,
    rate(T) = Q10 ** ((T - T_REF) / 10)        (1.0 at the reference storage temperature)
consumed_life = integral rate(T(t)) dt / nominal_life_at_T_REF.
The tag only reports a coarse sample every 6 h, so short excursions between samples are
invisible to a plain integral of the series. But the tag integrates time x temperature ON-CHIP
every minute and latches the FRESH/AGING/CRITICAL stage, so the stage is a free, fine-grained
side channel. The model gets: coarse-series features + the latched stage. Labels come from the
FINE (1-minute) true profile.

IMPORTANT: the shipped model is trained on synthetic data generated from this physics.
Re-train with real product data (fit(X, y)) before using remaining-life numbers commercially.
"""
import numpy as np

T_REF_C = 5.0                 # reference cold-chain temperature
ONCHIP_BASE_C = 8.0           # tag t_base: on-chip integrator counts only (T - base) above this
STAGE_MILD_CH = 60.0          # degC*h latched as AGING    (example register values; per-product in practice)
STAGE_SEVERE_CH = 200.0       # degC*h latched as CRITICAL
STORAGE_C = 5.0               # temperature assumed for the remaining life
SAMPLE_H = 6.0                # heartbeat period of the tag
PRODUCTS = {                  # nominal life at T_REF (hours), Q10
    "dairy":   (14 * 24, 3.0),
    "frozen":  (180 * 24, 2.5),
    "vaccine": (365 * 24, 2.0),
    "packaged": (120 * 24, 2.2),
}


def decode_temp(code: int, sensor_type: int = 0) -> float:
    """Tag sends an 8-bit sample. Default calibration: code == degrees C (see tag testbench
    where 8 = cold room, 30 = hot truck). Override per sensor_type if the sensor differs."""
    return float(code)


def rate(temp_c, q10):
    return np.power(q10, (np.asarray(temp_c, dtype=float) - T_REF_C) / 10.0)


def consumed_fraction(temps_c, dt_h, nominal_h, q10):
    return float(np.sum(rate(temps_c, q10)) * dt_h / nominal_h)


def features(series_c, nominal_h, q10, stage=0):
    s = np.asarray(series_c, dtype=float)
    cons = consumed_fraction(s, SAMPLE_H, nominal_h, q10)
    return np.array([
        cons, s.mean(), s.max(), s.std(), float(np.mean(s > T_REF_C + 3)) * len(s) * SAMPLE_H,
        len(s) * SAMPLE_H / nominal_h, s[-1], nominal_h, q10,
        float(np.max(np.diff(s))) if len(s) > 1 else 0.0, float(stage)], dtype=float)


def _remaining_h(consumed, nominal_h, q10):
    return max(0.0, 1.0 - consumed) * nominal_h / float(rate(STORAGE_C, q10))


def physics_remaining_h(series_c, nominal_h, q10):
    """Baseline without ML (also the fallback when scikit-learn is unavailable)."""
    return _remaining_h(consumed_fraction(series_c, SAMPLE_H, nominal_h, q10), nominal_h, q10)


def synth_dataset(n=4000, seed=7):
    rng = np.random.default_rng(seed)
    X, y = [], []
    prods = list(PRODUCTS.values())
    for _ in range(n):
        nominal_h, q10 = prods[rng.integers(len(prods))]
        days = rng.uniform(0.5, min(nominal_h / 24, 30))
        n_fine = int(days * 24 * 60)
        t = np.arange(n_fine) / 60.0
        base = rng.uniform(2, 8) + rng.normal(0, 0.5, n_fine).cumsum() * 0.01
        base += 1.5 * np.sin(2 * np.pi * t / 24 + rng.uniform(0, 6.28))        # daily cycle
        for _ in range(rng.poisson(1.2)):                                         # excursions
            c = rng.uniform(0, t[-1]); w = rng.uniform(0.3, 10); a = rng.uniform(5, 30)
            base += a * np.exp(-0.5 * ((t - c) / w) ** 2)
        fine_consumed = float(np.sum(rate(base, q10)) / 60.0 / nominal_h)
        ch = float(np.sum(np.maximum(0.0, base - ONCHIP_BASE_C)) / 60.0)        # on-chip degC*h
        stage = 2 if ch >= STAGE_SEVERE_CH else 1 if ch >= STAGE_MILD_CH else 0
        coarse = base[:: int(SAMPLE_H * 60)]
        if len(coarse) < 2:
            continue
        coarse = coarse + rng.normal(0, 0.3, len(coarse))                         # sensor noise
        X.append(features(coarse, nominal_h, q10, stage))
        y.append(_remaining_h(fine_consumed, nominal_h, q10) / nominal_h)         # fraction of nominal
    return np.array(X), np.array(y)


class ShelfLifeModel:
    """physics estimate + learned residual correction (gradient boosting, L1 loss)."""

    def __init__(self):
        self.model = None

    @staticmethod
    def _phys_frac(X):
        return np.clip(1.0 - X[:, 0], 0.0, 1.0)            # column 0 = coarse consumed fraction

    def fit(self, X, y):
        from sklearn.ensemble import GradientBoostingRegressor
        self.model = GradientBoostingRegressor(n_estimators=150, max_depth=2, learning_rate=0.03,
                                               subsample=0.8, loss="absolute_error",
                                               random_state=0).fit(X, y - self._phys_frac(X))
        return self

    def predict_frac(self, X):
        return np.clip(self._phys_frac(X) + self.model.predict(X), 0.0, 1.0)

    def fit_synthetic(self, n=6000, seed=1):
        X, y = synth_dataset(n, seed)
        return self.fit(X, y)

    def predict_hours(self, temps_c, product="dairy", stage=0):
        """Predicted remaining shelf life (hours at STORAGE_C) for one box."""
        nominal_h, q10 = PRODUCTS[product]
        if len(temps_c) < 2 or self.model is None:
            return physics_remaining_h(temps_c or [STORAGE_C], nominal_h, q10)
        x = features(temps_c, nominal_h, q10, stage).reshape(1, -1)
        return float(self.predict_frac(x)[0] * nominal_h)
