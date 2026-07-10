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

## Key Finding: Model Reliability Is Volume-Dependent

The single most important result from this project isn't a channel ranking —
it's that **the model's own out-of-sample reliability is strongly correlated
with each brand's conversion volume (r = 0.778)**:

| Brand | Conversions | Holdout Rank Correlation |
|---|---|---|
| B07 | 1,450 | **0.70** (strong) |
| B02 | 1,141 | **0.60** (strong) |
| B01 | 621 | 0.40 (moderate) |
| B09 | 179 | 0.30 (weak) |
| B10 | 280 | 0.05 (none) |
| B08 | 275 | **−0.46** (inverted) |
| B03 | 276 | **−0.36** (inverted) |

Brands with 1,000+ conversions validate well out-of-sample; brands with
under ~300 produce holdout rankings that are noisy or even inverted relative
to the training split. **Practical takeaway: this attribution method should
only be trusted above a conversion-volume threshold (roughly 500+ in this
dataset) — below that, a simpler heuristic is more honest than a
sophisticated model with unstable estimates.** This is a stronger, more
defensible claim than "the model works," because it states precisely where
it doesn't.

## Flagship Case Study: Brand B07

B07 has the largest conversion volume (1,450) and the most stable holdout
validation (rank correlation 0.70) in the portfolio — the brand where this
method's output can be trusted with the most confidence.

| Channel | Naive Last-Click % | True Attribution % | True CPA (₹) |
|---|---|---|---|
| Google Search | 33.15% | 32.13% | 56,432 |
| Influencer Blog | 19.89% | 19.53% | 10,950 |
| Instagram | 18.23% | 18.08% | 183,515 |
| YouTube | 15.47% | 15.73% | **10,071** |
| Marketplace | 13.26% | 14.53% | 95,874 |

The cost-efficiency gap between the cheapest channel (YouTube, ₹10,071 CPA)
and the most expensive (Instagram, ₹183,515 CPA) is **18.2×** — despite
Instagram and YouTube receiving similar attribution credit (18% vs 16%),
Instagram is dramatically less efficient at converting that credit into
actual purchases.

## Portfolio-Level Finding: No Universal Best Channel

Averaged across all 10 brands, true attribution is fairly evenly spread
(Instagram 23.5%, Influencer Blog 21.2%, Google Search 20.2%, Marketplace
19.7%, YouTube 15.4%) — but the **top recommended channel differs by
brand**: Instagram leads for B01/B06/B08, Influencer Blog for B02/B03/B10,
Marketplace for B04/B05, YouTube for B07, Google Search for B09. A portfolio-
wide "shift budget to Channel X" recommendation would be wrong for at least
half the brands it's applied to — the right level of action is per-brand,
not per-portfolio.

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
