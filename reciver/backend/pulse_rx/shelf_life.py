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
# nominal life at T_REF (hours), Q10.   EVIDENCE STATUS:
#  dairy   - Q10 5.0 from published pasteurised-milk studies (shelf life 30/24/12 d at 5/7/10 C; 28 -> 6 d from 5 to
#            11 C; 72/36/24 h at 13/15/19 C => Q10 ~5-6 below 20 C). Nominal 14 d = commercial milk (12-21 d, Cornell).
#  vaccine - Q10 3.5 derived from the WHO PQS default MKT activation energy 83.144 kJ/mol near 5 C. Nominal life is a
#            placeholder: real vaccines differ by product/VVM class - set per product from the manufacturer's data.
#  frozen, packaged - UNVERIFIED planning assumptions. Replace with product data before relying on them.
PRODUCTS = {
    "dairy":   (14 * 24, 5.0),
    "frozen":  (180 * 24, 2.5),
    "vaccine": (365 * 24, 3.5),
    "packaged": (120 * 24, 2.2),
}
RATE_CAP_C = 35.0             # microbial growth saturates; do not extrapolate Q10 beyond this temperature


# sensor_type -> (scale, offset): degC = code * scale + offset.
# 0/1 = legacy 1:1 (code == degC, cannot represent below 0 C, see tag testbench: 8 = cold room, 30 = hot truck).
# 2   = 0.5 C steps from -40 C (range -40..+87.5 C): REQUIRED to see freezing. Set the tag's sensor_type to 2.
SENSOR_CAL = {0: (1.0, 0.0), 1: (1.0, 0.0), 2: (0.5, -40.0)}


def decode_temp(code: int, sensor_type: int = 0) -> float:
    scale, offset = SENSOR_CAL.get(sensor_type, (1.0, 0.0))
    return float(code) * scale + offset


def mkt_c(temps_c, ea_j_mol=83144.0):
    """Mean kinetic temperature (degC), equal-interval samples. WHO PQS default Ea = 83.144 kJ/mol.
    Heat-weighted; says nothing about freezing."""
    R = 8.314462618
    t = np.asarray(temps_c, dtype=float) + 273.15
    return float((ea_j_mol / R) / -np.log(np.mean(np.exp(-ea_j_mol / (R * t)))) - 273.15)


# WHO PQS vaccine storage limits (per-sample checks; 6-hourly samples cannot verify the 1-hour freeze rule)
VACCINE_MAX_C = 20.0          # "no excursion must exceed +20 C for any amount of time"
VACCINE_FREEZE_C = -0.5       # "no excursion below -0.5 C for any amount of time"


def rate(temp_c, q10):
    t = np.minimum(np.asarray(temp_c, dtype=float), RATE_CAP_C)
    return np.power(q10, (t - T_REF_C) / 10.0)


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
        resid = y - self._phys_frac(X)
        mk = lambda **kw: GradientBoostingRegressor(n_estimators=150, max_depth=2, learning_rate=0.03,
                                                    subsample=0.8, random_state=0, **kw).fit(X, resid)
        self.model = mk(loss="absolute_error")
        self.lo = mk(loss="quantile", alpha=0.1)         # 10th / 90th percentile of the residual =>
        self.hi = mk(loss="quantile", alpha=0.9)         # an ~80 % prediction interval
        return self

    def predict_range_frac(self, X):
        base = self._phys_frac(X)
        lo = np.clip(base + self.lo.predict(X), 0, 1); hi = np.clip(base + self.hi.predict(X), 0, 1)
        mid = self.predict_frac(X)
        return np.minimum(lo, mid), mid, np.maximum(hi, mid)

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

    def predict_range(self, temps_c, product="dairy", stage=0):
        """(low, mid, high) remaining hours; ~80 % interval when the model is trained, else (mid, mid, mid)."""
        mid = self.predict_hours(temps_c, product, stage)
        if self.model is None or len(temps_c) < 2:
            return mid, mid, mid
        nominal_h, q10 = PRODUCTS[product]
        x = features(temps_c, nominal_h, q10, stage).reshape(1, -1)
        lo, m, hi = (float(v[0]) * nominal_h for v in self.predict_range_frac(x))
        return lo, m, hi
