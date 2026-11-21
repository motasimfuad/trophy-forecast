"""GTA VI trophy earn-rate forecaster for make.fun's gta6-trophy-rarity-forecast-01.

Reads every case file (*.in) in $MAKEFUN_INPUT_DIR (default /input) and writes one
forecast (<id>.out) per case to $MAKEFUN_OUTPUT_DIR (default /output): five quantiles
of each trophy's Exophase earn rate at the case's target.

Model (standard library only, deterministic):

* The history is collapsed into Exophase refreshes: consecutive reads with the same
  players-tracked count and the same rates are one refresh of the page.
* The launch-night page (before the first real refresh) is dropped.
* Each trophy's rate is mapped to probit space, z = Phi^-1(rate / 100), and fitted
  as a straight line against ln(hours since 2026-11-19T00:00Z). A lognormal spread
  of play speed across players makes every progress trophy a line in that space with
  one shared slope, so each trophy's own slope is shrunk toward a pooled slope; the
  rarer (noisier, rounded to 0.01%) a trophy is, the harder it is pulled.
* The line is read at the expected time of Exophase's last refresh before the target.
* The spread is calibrated inside the program: the same fit is run on the history cut
  some hours earlier and scored on the latest refresh; that error is scaled to the
  target's horizon in log-time.
"""

import json
import math
import os
import sys
from datetime import datetime, timezone
from statistics import NormalDist

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
ND = NormalDist()
ZL = [ND.inv_cdf(p) for p in LEVELS]
ORIGIN = datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)
MIN_HOURS = 4.0          # reads before this are the pre-launch page Exophase had not refreshed
WINDOW_HOURS = 40.0      # fit on refreshes this recent
DECAY_HOURS = 20.0       # recency weight of a refresh
ROUND_SD = 0.01 / math.sqrt(12.0)  # the page rounds to 0.01 percentage points
SLOPE_PRIOR_SD = 0.30    # between-trophy spread of probit slopes around the pooled one
DAMP = 0.95              # mild hedge toward persistence
Z_FLOOR = -4.4           # probit of ~0.0005%
Z_CEIL = 4.4


def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def hours_of(dt):
    return (dt - ORIGIN).total_seconds() / 3600.0


def to_z(rate):
    p = min(max(rate, 0.0005), 99.9995) / 100.0
    return ND.inv_cdf(p)


def from_z(z):
    return 100.0 * ND.cdf(z)


def z_noise(rate):
    """Standard deviation, in probit units, of the 0.01% rounding at this rate."""
    z = to_z(rate)
    dens = ND.pdf(z)
    return (ROUND_SD / 100.0) / max(dens, 1e-12)


def collapse(history, n):
    """Forward-filled reads collapsed into distinct refreshes: (hours, players, rates)."""
    cur = [None] * n
    snaps = []
    key_prev = None
    for read in history:
        rates = read.get("rates") or []
        for i in range(n):
            v = rates[i] if i < len(rates) else None
            if isinstance(v, (int, float)) and math.isfinite(v):
                cur[i] = float(v)
        vals = tuple(cur)
        key = (read.get("players_tracked"), vals)
        if key != key_prev:
            snaps.append((hours_of(parse_time(read["at"])), read.get("players_tracked"), vals))
            key_prev = key
    return snaps, cur


def wls(xs, ys, ws):
    sw = sum(ws)
    if sw <= 0 or len(xs) < 2:
        return None
    mx = sum(w * x for x, w in zip(xs, ws)) / sw
    my = sum(w * y for y, w in zip(ys, ws)) / sw
    sxx = sum(w * (x - mx) ** 2 for x, w in zip(xs, ws))
    if sxx <= 1e-12:
        return None
    sxy = sum(w * (x - mx) * (y - my) for x, y, w in zip(xs, ys, ws))
    b = sxy / sxx
    a = my - b * mx
    return a, b, sxx, sw


def fit(snaps, n):
    """Per-trophy probit line over the recent refreshes, slopes shrunk to a pooled slope.

    Returns (t_last, z_last, slope) lists, or None if there is too little history."""
    use = [s for s in snaps if s[0] >= MIN_HOURS]
    if len(use) < 3:
        return None
    t_last = use[-1][0]
    win = [s for s in use if s[0] >= t_last - WINDOW_HOURS]
    if len(win) < 3:
        win = use[-3:]
    xs_all = [math.log(s[0]) for s in win]
    tw = [math.exp(-(t_last - s[0]) / DECAY_HOURS) for s in win]
    raw = []
    for i in range(n):
        xs, ys, ws = [], [], []
        for s, x, w in zip(win, xs_all, tw):
            r = s[2][i]
            if r is None or r <= 0.0 or r >= 100.0:
                continue
            nz = z_noise(r)
            xs.append(x)
            ys.append(to_z(r))
            ws.append(w / (nz * nz + 0.01 ** 2))
        res = wls(xs, ys, ws)
        if res is None:
            raw.append(None)
            continue
        a, b, sxx, sw = res
        # slope variance: residual scatter (at least the rounding noise) over the leverage
        resid = sum(w * (y - a - b * x) ** 2 for x, y, w in zip(xs, ys, ws))
        dof = max(len(xs) - 2, 1)
        sigma2 = max(resid / dof, 1.0)
        raw.append((a, b, sigma2 / sxx))
    # pooled slope: precision-weighted mean of the well-measured trophies
    num = den = 0.0
    for r in raw:
        if r is None:
            continue
        _, b, vb = r
        w = 1.0 / (vb + SLOPE_PRIOR_SD ** 2)
        num += w * b
        den += w
    pooled = num / den if den > 0 else 0.0
    last_rates = use[-1][2]
    z_last, slopes = [], []
    for i in range(n):
        r_last = last_rates[i]
        r = raw[i]
        if r is None:
            slopes.append(pooled * 0.5)
            z_last.append(to_z(r_last if r_last is not None else 0.0))
            continue
        a, b, vb = r
        prec_own = 1.0 / vb
        prec_prior = 1.0 / SLOPE_PRIOR_SD ** 2
        b_s = (prec_own * b + prec_prior * pooled) / (prec_own + prec_prior)
        # level now: blend the observed last rate with the fitted line by their noise
        z_obs = to_z(r_last)
        z_fit = a + b * math.log(t_last)
        nz = z_noise(r_last)
        w_obs = 1.0 / (nz * nz + 1e-4)
        w_fit = 1.0 / (0.05 ** 2)
        z_last.append((w_obs * z_obs + w_fit * z_fit) / (w_obs + w_fit))
        slopes.append(b_s)
    return t_last, z_last, slopes, pooled


def persistence(case, last, spread):
    rows = []
    for t, r in zip(case["trophies"], last):
        r = 0.0 if r is None else r
        rows.append({"id": t["id"], "q": quantiles_from_logit(r, spread)})
    return rows


def quantiles_from_logit(r, s):
    c = min(max(r, 0.01), 99.99)
    lg = math.log(c / (100.0 - c))
    return [round(100.0 / (1.0 + math.exp(-(lg + z * s))), 4) for z in ZL]


def finish(qs):
    out = []
    prev = 0.0
    for q in qs:
        q = min(max(q, 0.0), 100.0)
        q = round(q, 4)
        if q < prev:
            q = prev
        out.append(q)
        prev = q
    return out


def forecast(case):
    trophies = case["trophies"]
    n = len(trophies)
    target_h = hours_of(parse_time(case["target"]))
    snaps, last = collapse(case.get("history") or [], n)
    horizon_days = float(case.get("horizon_minutes", 0)) / 1440.0
    model = fit(snaps, n)
    if model is None:
        # no refresh after launch night: a wide random walk around the last read
        return persistence(case, last, 0.8 + 0.5 * math.sqrt(horizon_days))
    t_last, z_last, slopes, pooled = model
    use = [s for s in snaps if s[0] >= MIN_HOURS]
    gaps = sorted(b[0] - a[0] for a, b in zip(use[-8:], use[-7:]))
    gap = gaps[len(gaps) // 2] if gaps else 1.0
    gap = min(max(gap, 0.25), 12.0)
    t_eff = max(target_h - gap / 2.0, t_last)
    dx = math.log(t_eff / t_last)

    # calibration: refit on the history cut earlier, score it on the latest refresh
    s_cal = None
    for cut in (12.0, 18.0, 8.0):
        early = [s for s in snaps if s[0] <= t_last - cut]
        m2 = fit(early, n)
        if m2 is None:
            continue
        t2, z2, b2, _ = m2
        dx2 = math.log(t_last / t2)
        if dx2 <= 0.02:
            continue
        errs = []
        for i in range(n):
            r = use[-1][2][i]
            if r is None or r < 0.05 or r > 99.9:
                continue
            pred = z2[i] + DAMP * b2[i] * dx2
            errs.append(abs(to_z(r) - pred))
        if len(errs) >= 5:
            errs.sort()
            mad = errs[len(errs) // 2]
            s_cal = 1.4826 * mad * (dx / dx2)
            break
    if s_cal is None:
        s_cal = 0.35 * dx / 0.9
    s_base = min(max(s_cal, 0.12 + 0.18 * dx), 0.9)

    rows = []
    for i, t in enumerate(trophies):
        r_last = last[i]
        if r_last is None:
            r_last = 0.0
        if r_last >= 99.995:
            rows.append({"id": t["id"], "q": [100.0] * 5})
            continue
        if r_last <= 0.0:
            # nobody tracked has it yet: it stays at the 0.01% floor unless it starts to move
            z0 = to_z(0.003)
            zm = z0 + 0.5 * max(pooled, 0.0) * DAMP * dx
            s = s_base + 0.25
            qs = [from_z(min(max(zm + zq * (s * (1.3 if zq > 0 else 0.6)), Z_FLOOR), Z_CEIL)) for zq in ZL]
            rows.append({"id": t["id"], "q": finish(qs)})
            continue
        zm = z_last[i] + DAMP * slopes[i] * dx
        s = math.sqrt(s_base ** 2 + z_noise(r_last) ** 2)
        up = 1.15 if r_last < 10.0 else 1.0
        dn = 0.9 if r_last < 10.0 else 1.0
        qs = []
        for zq in ZL:
            k = up if zq > 0 else dn
            qs.append(from_z(min(max(zm + zq * s * k, Z_FLOOR), Z_CEIL)))
        rows.append({"id": t["id"], "q": finish(qs)})
    return rows


def main():
    in_dir = os.environ.get("MAKEFUN_INPUT_DIR") or "/input"
    out_dir = os.environ.get("MAKEFUN_OUTPUT_DIR") or "/output"
    os.makedirs(out_dir, exist_ok=True)
    for name in sorted(f for f in os.listdir(in_dir) if f.endswith(".in")):
        with open(os.path.join(in_dir, name), encoding="utf-8") as fh:
            case = json.load(fh)
        try:
            rows = forecast(case)
        except Exception as exc:  # never leave a case without a valid forecast
            sys.stderr.write("%s: model failed (%r), persistence used\n" % (name, exc))
            _, last = collapse(case.get("history") or [], len(case["trophies"]))
            rows = persistence(case, last, 0.5 * math.sqrt(float(case.get("horizon_minutes", 0)) / 1440.0))
        with open(os.path.join(out_dir, name[:-3] + ".out"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"trophies": rows}, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
