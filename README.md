# ROI Lens — Multi-Touch Attribution & Budget Reallocation Engine

A probabilistic attribution pipeline that replaces last-click heuristics with
Markov-chain removal-effect modeling — run independently across a 10-brand
portfolio (100,000 users, 566K+ raw events), with statistical validation at
every stage rather than a single asserted result.

## The Problem

Last-click attribution — crediting 100% of a conversion to whichever channel
touched the user last — ignores every earlier touchpoint in the funnel. It's
the default in most marketing stacks precisely because it's cheap to compute,
not because it's accurate. ROI Lens quantifies exactly how wrong it is, per
brand, and recommends a statistically-grounded reallocation of each brand's
media budget.

## Architecture

```
Raw Events (566K+ rows: Impression, Click, Add-to-Cart, Purchase)
      │
      ▼
┌─────────────────────────────────────────┐
│  SQL ETL Layer (SQLite)                  │
│  • CTE + window functions for bot         │
│    detection (volume + click-velocity)   │
│  • Journey-path construction              │
└─────────────────────────────────────────┘
      │
      ▼  (split by Brand_ID — 10 brands, independent budgets)
      │
┌─────────────────────────────────────────┐
│  Per-Brand Markov Chain Attribution       │
│  • Journey built from ENGAGEMENT events   │
│    only (Click/Cart/Purchase) — passive   │
│    impressions excluded from the chain    │
│  • Removal-effect via fundamental matrix  │
│    inversion: N = (I − Q)⁻¹               │
└─────────────────────────────────────────┘
      │
      ├──► Naive last-click baseline (comparison)
      ├──► Bootstrap resampling (500 iters/brand → 95% CI)
      ├──► Time-based holdout validation (train vs test, same units)
      │
      ▼
┌─────────────────────────────────────────┐
│  Per-Brand CPA + Fatigue-Adjusted Budget  │
│  • True CPA per channel, per brand        │
│  • √-dampened reallocation against each   │
│    brand's ACTUAL budget                  │
└─────────────────────────────────────────┘
      │
      ▼
Portfolio Rollup (10 brands) + Power BI Dashboard
```

## Why Per-Brand, Not Pooled

The dataset spans **10 brands** (`U_B01_...` through `U_B10_...`), each with
its own ~10,000 users and its own ~₹10 Crore budget. An earlier version of
this pipeline pooled all 10 brands into a single population before computing
attribution and CPA — which blends 10 unrelated campaigns' spend and channel
mix into one meaningless average. Every number below is computed **per
brand, independently**, with a portfolio-level rollup computed as an average
across brands — a legitimate question — rather than a pooled recomputation,
which is not.

Journey construction also excludes passive **Impressions** (77.6% of all raw
events) from the Markov chain itself. Impressions are still used for bot
detection — a bot inflating impression counts is a real signal — but
treating an untouched ad exposure as equivalent to a Click in the transition
matrix would dilute the model with noise the person never acted on.
Purchase events are treated as the terminal touchpoint: the Purchase row's
Channel is retained in the journey before the absorbing Conversion state.
This preserves the final marketing touch used for both naive last-click and
Markov attribution.


## Key Finding: Model Reliability Is Volume-Dependent

The single most important result from this project isn't a channel ranking —
it's that **the model's own out-of-sample reliability is positively correlated
with each brand's conversion volume (r = 0.529)**:

| Brand | Conversions | Holdout Rank Correlation |
|---|---:|---:|
| B07 | 1,450 | **1.00** (strong) |
| B01 | 621 | **0.87** (strong) |
| B10 | 280 | 0.70 (moderate) |
| B09 | 179 | 0.60 (moderate) |
| B02 | 1,141 | 0.30 (weak) |
| B03 | 276 | 0.30 (weak) |
| B04 | 278 | 0.30 (weak) |
| B05 | 147 | 0.20 (weak) |
| B06 | 267 | 0.10 (weak) |
| B08 | 275 | 0.10 (weak) |

The relationship is positive but imperfect: higher conversion volume generally
provides more stable holdout rankings, while several lower-volume brands still
produce weak or unstable estimates. **Practical takeaway: treat attribution
outputs as more reliable when conversion volume is high; for low-volume brands,
a simpler heuristic may be more appropriate than relying heavily on a noisy
multi-touch estimate.**

## Flagship Case Study: Brand B07

B07 has the largest conversion volume (1,450) and the strongest holdout
validation (rank correlation 1.00) in the portfolio — the brand where this
method's output can be trusted with the most confidence.

| Channel | Naive Last-Click % | True Attribution % | True CPA (₹) |
|---|---:|---:|---:|
| Google Search | 69.10% | 64.85% | 27,960 |
| Marketplace | 24.00% | 23.29% | 59,821 |
| Instagram | 2.55% | 4.27% | 777,053 |
| YouTube | 2.41% | 3.90% | **40,666** |
| Influencer Blog | 1.93% | 3.70% | 57,821 |

The cost-efficiency gap between YouTube (₹40,666 CPA) and Instagram
(₹777,053 CPA) is **19.1×** despite both receiving relatively small shares
of naive last-click credit. The Markov model gives more credit to Instagram,
YouTube, and Influencer Blog than naive last-click, illustrating how
multi-touch attribution can redistribute credit away from the dominant
last-click channel.

## Portfolio-Level Finding: No Universal Best Channel

Averaged across all 10 brands, true attribution is concentrated in Google Search
(28.45%) and Marketplace (24.58%), followed by Instagram (19.09%),
Influencer Blog (15.76%), and YouTube (12.12%) — but the **top recommended
channel differs by brand**: Instagram leads for B01/B06, Influencer Blog for
B02/B03/B10, Marketplace for B04/B05/B09, and Google Search for B07/B08.
A portfolio-wide "shift budget to Channel X" recommendation would therefore
be misleading; the right level of action is per-brand, not per-portfolio.

## Limitations (Stated Proactively)

- **Reliability is volume-dependent** (see above) — treat low-conversion
  brands' recommendations as directional, not decisive.
- Removal effects are computed independently per channel and normalized to
  100%, assuming limited interaction between channels — a known simplification
  of this attribution family.
- The fatigue-dampening exponent (default √, i.e. 0.5) is a modeling
  assumption, not empirically fit; it's exposed as a Power BI What-If
  parameter specifically so it's adjustable rather than silently fixed.
- Bot thresholds (500 actions, 1-second click interval) are heuristic, not
  tuned via sensitivity analysis on this specific dataset.
- Holdout validation checks whether channel *rankings* hold up out-of-sample;
  it is not a randomized controlled experiment.

## Tech Stack

- **Language:** Python
- **ETL:** SQLite (CTEs, window functions)
- **Analysis:** Pandas, NumPy (linear algebra, bootstrap resampling)
- **Dashboard:** Power BI (What-If parameter, per-brand + portfolio views)
- **Concepts:** Markov chains, absorbing-state probability matrices, bootstrap
  confidence intervals, time-based holdout validation, portfolio segmentation

## Setup

```bash
pip install pandas numpy
python roi_lens_v2.py
```

Expects `data/touchpoints.csv`, `data/user_profiles.csv`,
`data/campaign_spend.csv`. Outputs per-brand CSVs to
`powerbi_exports/<BRAND_ID>/` plus two portfolio rollups.

## Repository Structure

```
roi-lens/
├── data/                                  # input CSVs (not included)
├── roi_lens_v2.py                         # full pipeline
├── powerbi_exports/
│   ├── bot_funnel_summary.csv
│   ├── bot_summary_by_brand.csv
│   ├── user_journey_paths.csv
│   ├── portfolio_summary_by_brand.csv     # one row per brand
│   ├── portfolio_channel_view.csv         # avg attribution across brands
│   └── <BRAND_ID>/
│       ├── naive_vs_true_attribution.csv
│       ├── attribution_with_confidence_intervals.csv
│       ├── holdout_validation.csv
│       └── final_budget_allocation.csv
└── README.md
```

## Future Work

- Empirically fit the fatigue-dampening exponent per brand rather than
  assuming √ globally
- Extend removal-effect calculation to joint (multi-channel) removal to test
  the channel-independence assumption directly
- Formalize the volume-reliability threshold (currently an observed pattern,
  not a statistically derived cutoff) via power analysis
