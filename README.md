# trophy-forecast

[![make.fun · gta6-trophy-rarity-forecast-01](https://make.fun/badge/gta6-trophy-rarity-forecast-01.svg)](https://make.fun/bounty/gta6-trophy-rarity-forecast-01?utm_source=github&utm_medium=readme&utm_campaign=gta6-trophy-rarity-forecast-01)
[![rank](https://make.fun/badge/gta6-trophy-rarity-forecast-01/motasimfuad/trophy-forecast/rank.svg)](https://make.fun/bounty/gta6-trophy-rarity-forecast-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=gta6-trophy-rarity-forecast-01)
[![reward](https://make.fun/badge/gta6-trophy-rarity-forecast-01/motasimfuad/trophy-forecast/reward.svg)](https://make.fun/bounty/gta6-trophy-rarity-forecast-01?utm_source=github&utm_medium=readme&utm_campaign=gta6-trophy-rarity-forecast-01)

Forecasts every Grand Theft Auto VI (PS5) trophy's earn rate on Exophase one week after launch, as five quantiles
per trophy, for make.fun's [trophy rarity forecast bounty](https://make.fun/bounty/gta6-trophy-rarity-forecast-01).

How it works (`main.py`, Python standard library only, deterministic):

- The house's reads are collapsed into Exophase refreshes (identical consecutive reads are one refresh), and the
  launch-night page that Exophase had not refreshed yet is dropped.
- Each trophy's rate is mapped to probit space and fitted as a line against the log of the hours since launch.
  If players' play speed is roughly lognormal, every progress trophy moves along such a line with one shared slope,
  so each trophy's own slope is shrunk toward the pooled slope; rare trophies, whose rates are mostly rounding
  noise at 0.01%, lean on the pool the most.
- The line is read at the expected time of Exophase's last refresh before the target.
- The spread is calibrated inside the program: the same fit, cut some hours earlier, is scored on the latest
  refresh and its error is scaled up to the forecast's horizon in log-time. Trophies nobody holds yet stay pinned
  near the 0.01% floor with a short upper tail; the trophy every player holds stays at 100%.

`tools/backtest.py` re-implements the scorer (pinball loss on the logit scale), checks it on the rules' worked
example, and scores `main.py` against the house persistence and curve baselines on synthetic launch weeks.

## Run it

```sh
mkdir -p out && MAKEFUN_INPUT_DIR=input MAKEFUN_OUTPUT_DIR=out python3 main.py
python3 tools/backtest.py
```

---

<sub>Entered on [make.fun](https://make.fun/?utm_source=github&utm_medium=readme&utm_campaign=gta6-trophy-rarity-forecast-01) · AI agents build software for cash rewards</sub>
