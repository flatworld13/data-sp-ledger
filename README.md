# VERDICT by DATA-SP

Macro prediction scoring · SHA-256 timestamped · Brier calibrated · Primary source only

---

## Live track record

| Event | Date | Signal | Outcome | Correct | Brier |
|-------|------|--------|---------|---------|-------|
| CPI March 2026 | 2026-04-10 | MISS · 0.729 | Confirmed MISS | ✓ | 0.074 |

**Live events: 1 · Correct: 1 · Brier: 0.074**

Full ledger: [`public_ledger.json`](./public_ledger.json)

---

## Simulated backtest — formula v1.0

Formula locked: 2026-04-16 · [GitHub commit](./public_ledger.json)
Events scored: 123 · 61 CPI · 62 NFP
Period: 2021–2026
Source: api.bls.gov primary data

| Metric | All events | CPI only | NFP only |
|--------|-----------|---------|---------|
| Events | 123 | 61 | 62 |
| Accuracy | 87.0% | 73.8% | 100% |
| Brier score | 0.101 | 0.145 | 0.058 |
| Random baseline | 0.250 | 0.250 | 0.250 |
| High conviction >0.80 | 13/13 · 100% | 13/13 · 100% | 0 events |

Full backtest: [`verdict_backtest_results.json`](./verdict_backtest_results.json)

> Simulated backtest — formula weights locked before running.
> Simulated results do not guarantee future performance.

---

## How it works

Every macro release has two numbers.

The prediction — what economists forecast in advance.
The actual — what the government measured and published.

VERDICT is the judge sitting between them. It scores the gap, verifies the source, timestamps the verdict before markets reprice, and publishes the score publicly so any bot can act with calibrated confidence.

**VERDICT does not predict. It scores predictions.**

---

## Formula v1.0
