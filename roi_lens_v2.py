"""
ROI Lens v2 — Upgraded Multi-Touch Attribution Engine
========================================================
Adds on top of the original notebook:
  1. SQL-based ETL layer (SQLite, window functions + CTEs) for bot filtering
     and journey-path construction -> real, demonstrable SQL skill.
  2. Bootstrap confidence intervals on attribution weights (500 resamples per brand)
     -> turns a point estimate into a statistically defensible claim.
  3. Time-based holdout validation (quasi-experimental check) -> the honest
     substitute for A/B testing when you don't have a live experiment.
  4. Power BI-ready CSV exports, including a fatigue-exponent sweep table
     built for a Power BI "What-If Parameter" slider.

Core Markov / removal-effect math is UNCHANGED from the original notebook —
only the ETL stage and the analysis layer around it have been upgraded.
"""

import os
import sqlite3
from collections import defaultdict

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
DATA_DIR = "data"
OUTPUT_DIR = "powerbi_exports"
BUDGET_PER_BRAND = 100_000_000          # ₹10 Crore
N_BOOTSTRAP = 500                        # per brand (10 brands x 500 = 5,000 total resamples)
HOLDOUT_FRACTION = 0.2
FATIGUE_EXPONENTS_TO_SWEEP = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]

# Only these event types count as genuine engagement for journey construction.
# Impressions (passive ad exposure) are still used for bot detection, but are
# excluded from the Markov chain itself — a bot inflating impression counts
# is a real signal; a real user's untouched impression is not a "touchpoint"
# in the causal sense the attribution model is trying to measure.
ENGAGEMENT_EVENT_TYPES = ["Click", "Add-to-Cart", "Purchase"]


# ---------------------------------------------------------------------------
# STEP 1 — SQL-BASED ETL (bot filtering + journey construction)
# ---------------------------------------------------------------------------
def build_sql_pipeline(touchpoints_df: pd.DataFrame, profiles_df: pd.DataFrame):
    """Runs bot detection and journey-path extraction natively in SQL
    (SQLite in-memory) using window functions and CTEs, instead of the
    original pandas groupby().diff() approach."""

    conn = sqlite3.connect(":memory:")
    touchpoints_df.to_sql("touchpoints", conn, index=False, if_exists="replace")

    # CTE + window functions: LAG for click-speed, COUNT OVER for volume
    bot_filter_sql = """
    WITH ranked AS (
        SELECT *,
            (julianday(Timestamp) - julianday(
                LAG(Timestamp) OVER (PARTITION BY User_ID ORDER BY Timestamp)
            )) * 86400.0 AS time_diff_sec,
            COUNT(*) OVER (PARTITION BY User_ID) AS user_action_count
        FROM touchpoints
    ),
    bots AS (
        SELECT DISTINCT User_ID FROM ranked
        WHERE time_diff_sec < 1.0 OR user_action_count > 500
    )
    SELECT r.User_ID, r.Timestamp, r.Channel, r.Event_Type
    FROM ranked r
    LEFT JOIN bots b ON r.User_ID = b.User_ID
    WHERE b.User_ID IS NULL
    ORDER BY r.User_ID, r.Timestamp;
    """
    clean_df = pd.read_sql_query(bot_filter_sql, conn, parse_dates=["Timestamp"])

    # Bot funnel summary — feeds a Power BI funnel/waterfall chart
    bot_summary_sql = """
    WITH ranked AS (
        SELECT *,
            (julianday(Timestamp) - julianday(
                LAG(Timestamp) OVER (PARTITION BY User_ID ORDER BY Timestamp)
            )) * 86400.0 AS time_diff_sec,
            COUNT(*) OVER (PARTITION BY User_ID) AS user_action_count
        FROM touchpoints
    )
    SELECT
        COUNT(DISTINCT User_ID) AS total_users,
        COUNT(DISTINCT CASE WHEN user_action_count > 500 THEN User_ID END) AS volume_bot_users,
        COUNT(DISTINCT CASE WHEN time_diff_sec < 1.0 THEN User_ID END) AS speed_bot_users,
        COUNT(*) AS total_touchpoints
    FROM ranked;
    """
    bot_summary = pd.read_sql_query(bot_summary_sql, conn)

    # Journey path construction in SQL — human-readable paths for a dashboard
    journey_sql = """
    SELECT User_ID,
           GROUP_CONCAT(Channel, ' -> ') AS journey_path,
           MAX(CASE WHEN Event_Type = 'Purchase' THEN 1 ELSE 0 END) AS converted
    FROM (SELECT * FROM touchpoints ORDER BY User_ID, Timestamp)
    GROUP BY User_ID;
    """
    journey_paths = pd.read_sql_query(journey_sql, conn)

    conn.close()

    if "user_id" in profiles_df.columns:
        profiles_df = profiles_df.rename(columns={"user_id": "User_ID"})
    merged = pd.merge(clean_df, profiles_df, on="User_ID", how="left")

    # Extract Brand_ID from User_ID (e.g. "U_B01_00000" -> "B01"). Nexus Brands
    # is a portfolio of brands, each with its own users and its own budget —
    # everything downstream must be computed per-brand, not pooled.
    merged["Brand_ID"] = merged["User_ID"].str.extract(r"(B\d+)")

    bot_by_brand_sql = """
    WITH ranked AS (
        SELECT *,
            (julianday(Timestamp) - julianday(
                LAG(Timestamp) OVER (PARTITION BY User_ID ORDER BY Timestamp)
            )) * 86400.0 AS time_diff_sec,
            COUNT(*) OVER (PARTITION BY User_ID) AS user_action_count
        FROM touchpoints
    )
    SELECT
        SUBSTR(User_ID, INSTR(User_ID, 'B'), 3) AS Brand_ID,
        COUNT(DISTINCT User_ID) AS total_users,
        COUNT(DISTINCT CASE WHEN user_action_count > 500 THEN User_ID END) AS volume_bot_users,
        COUNT(DISTINCT CASE WHEN time_diff_sec < 1.0 THEN User_ID END) AS speed_bot_users
    FROM ranked
    GROUP BY Brand_ID;
    """
    conn2 = sqlite3.connect(":memory:")
    touchpoints_df.to_sql("touchpoints", conn2, index=False, if_exists="replace")
    bot_by_brand = pd.read_sql_query(bot_by_brand_sql, conn2)
    conn2.close()

    return merged, bot_summary, journey_paths, bot_by_brand


# ---------------------------------------------------------------------------
# STEP 2 — MARKOV CHAIN REMOVAL-EFFECT ATTRIBUTION (core logic preserved)
# ---------------------------------------------------------------------------
def build_journeys(merged_df: pd.DataFrame):
    """Builds Markov journeys from genuine engagement events only
    (Click, Add-to-Cart, Purchase). Passive Impressions are excluded here —
    they're still used upstream for bot detection, but including them in the
    journey chain would treat untouched ad exposure as equivalent to an
    actual user action, diluting the transition matrix with noise."""
    engagement_df = merged_df[merged_df["Event_Type"].isin(ENGAGEMENT_EVENT_TYPES)]
    engagement_df = engagement_df.sort_values(by=["User_ID", "Timestamp"])
    journeys = []
    for _, group in engagement_df.groupby("User_ID"):
        events = group["Event_Type"].values
        channels = group["Channel"].values
        journey = ["Start"]
        converted = False
        for ch, ev in zip(channels, events):
            journey.append(ch)
            if ev == "Purchase":
                converted = True
                break
        journey.append("Conversion" if converted else "Null")
        journeys.append(journey)
    return journeys


def compute_removal_effects(journeys):
    transitions = defaultdict(int)
    state_counts = defaultdict(int)
    states = set()

    for journey in journeys:
        for i in range(len(journey) - 1):
            a, b = journey[i], journey[i + 1]
            transitions[(a, b)] += 1
            state_counts[a] += 1
            states.add(a)
            states.add(b)

    states = list(states)
    matrix_df = pd.DataFrame(0.0, index=states, columns=states)
    for (a, b), count in transitions.items():
        matrix_df.loc[a, b] = count / state_counts[a]

    if "Conversion" in matrix_df.index:
        matrix_df.loc["Conversion", :] = 0.0
        matrix_df.loc["Conversion", "Conversion"] = 1.0
    if "Null" in matrix_df.index:
        matrix_df.loc["Null", :] = 0.0
        matrix_df.loc["Null", "Null"] = 1.0

    total_journeys = len(journeys)
    base_conversions = sum(1 for j in journeys if j[-1] == "Conversion")
    base_conversion_rate = base_conversions / total_journeys if total_journeys else 0

    marketing_channels = [s for s in states if s not in ["Start", "Conversion", "Null"]]
    transient_states = [s for s in states if s not in ["Conversion", "Null"]]
    absorbing_states = ["Conversion", "Null"]
    ordered_states = transient_states + absorbing_states
    start_idx = transient_states.index("Start") if "Start" in transient_states else 0
    conv_idx = absorbing_states.index("Conversion")

    removal_effects = {}
    for channel in marketing_channels:
        removal_matrix = matrix_df.copy()
        removal_matrix.loc[channel, :] = 0.0
        removal_matrix.loc[channel, "Null"] = 1.0

        P = removal_matrix.loc[ordered_states, ordered_states].values
        t = len(transient_states)
        Q = P[:t, :t]
        R = P[:t, t:]
        try:
            N = np.linalg.inv(np.eye(t) - Q)
            B = N.dot(R)
            new_conv_prob = B[start_idx, conv_idx]
        except np.linalg.LinAlgError:
            new_conv_prob = 0.0

        removal_effect = (
            (base_conversion_rate - new_conv_prob) / base_conversion_rate
            if base_conversion_rate else 0
        )
        removal_effects[channel] = removal_effect

    total_effect = sum(removal_effects.values()) or 1
    attribution_weights = {k: (v / total_effect) * 100 for k, v in removal_effects.items()}
    return attribution_weights, base_conversions, base_conversion_rate


# ---------------------------------------------------------------------------
# STEP 2.5 — NAIVE LAST-CLICK BASELINE (the comparison your "undervalued by
# X%" claim actually depends on — without this, that number is unproven)
# ---------------------------------------------------------------------------
def compute_naive_last_click(journeys):
    """Standard last-click heuristic: the touchpoint immediately before a
    Conversion gets 100% of the credit. This is the baseline model ROI Lens
    is designed to replace."""
    counts = defaultdict(int)
    credited_conversions = 0  # conversions with at least one real marketing touch
    for journey in journeys:
        if journey[-1] == "Conversion" and len(journey) >= 2:
            last_channel = journey[-2]
            if last_channel == "Start":
                continue  # zero-touch conversion — no channel to credit, excluded
                          # from both models for a fair comparison
            credited_conversions += 1
            counts[last_channel] += 1

    if credited_conversions == 0:
        return {}
    return {ch: (c / credited_conversions) * 100 for ch, c in counts.items()}


def build_naive_vs_true_comparison(attribution_weights, naive_weights):
    channels = set(attribution_weights) | set(naive_weights)
    rows = []
    for ch in channels:
        true_w = attribution_weights.get(ch, 0.0)
        naive_w = naive_weights.get(ch, 0.0)
        pct_diff = ((true_w - naive_w) / naive_w * 100) if naive_w > 0 else np.nan
        rows.append({
            "Channel": ch,
            "Naive_LastClick_%": round(naive_w, 2),
            "True_Markov_Attribution_%": round(true_w, 2),
            "Relative_Difference_%": round(pct_diff, 1) if pd.notna(pct_diff) else np.nan,
        })
    return pd.DataFrame(rows).sort_values("True_Markov_Attribution_%", ascending=False)


def compute_cpa_naive_vs_true(attribution_weights, naive_weights, base_conversions, spend_df):
    historical_spend = spend_df.groupby("Channel")["Total_Budget_Allocated"].sum()
    rows = []
    channels = set(attribution_weights) | set(naive_weights)
    for ch in channels:
        spend = historical_spend.get(ch, 0)
        true_conv = base_conversions * (attribution_weights.get(ch, 0) / 100)
        naive_conv = base_conversions * (naive_weights.get(ch, 0) / 100)
        true_cpa = spend / true_conv if true_conv > 0 else np.nan
        naive_cpa = spend / naive_conv if naive_conv > 0 else np.nan
        rows.append({
            "Channel": ch,
            "Naive_LastClick_CPA": round(naive_cpa, 2) if pd.notna(naive_cpa) else np.nan,
            "True_Markov_CPA": round(true_cpa, 2) if pd.notna(true_cpa) else np.nan,
        })
    return pd.DataFrame(rows).sort_values("True_Markov_CPA")


# ---------------------------------------------------------------------------
# STEP 3 — BOOTSTRAP CONFIDENCE INTERVALS
# ---------------------------------------------------------------------------

def bootstrap_attribution_ci(journeys, n_bootstrap=N_BOOTSTRAP, seed=42):
    rng = np.random.default_rng(seed)
    n = len(journeys)
    all_weights = defaultdict(list)

    for _ in range(n_bootstrap):
        sample_idx = rng.integers(0, n, size=n)
        sample_journeys = [journeys[i] for i in sample_idx]
        try:
            weights, _, _ = compute_removal_effects(sample_journeys)
            for ch, w in weights.items():
                all_weights[ch].append(w)
        except Exception:
            continue

    records = []
    for ch, vals in all_weights.items():
        vals = np.array(vals)
        records.append({
            "Channel": ch,
            "Mean_Attribution_%": round(float(np.mean(vals)), 2),
            "CI_Lower_2.5%": round(float(np.percentile(vals, 2.5)), 2),
            "CI_Upper_97.5%": round(float(np.percentile(vals, 97.5)), 2),
        })
    return pd.DataFrame(records).sort_values("Mean_Attribution_%", ascending=False)


# ---------------------------------------------------------------------------
# STEP 4 — TIME-BASED HOLDOUT VALIDATION (quasi-experimental check)
# ---------------------------------------------------------------------------
def time_based_holdout(merged_df: pd.DataFrame, holdout_fraction=HOLDOUT_FRACTION):
    """Splits by date, then computes the SAME true-Markov-attribution metric
    independently on both halves. Comparing a % share to a % share (not a
    share to a raw rate) is what makes this an apples-to-apples validation."""
    merged_df = merged_df.sort_values("Timestamp")
    cutoff_idx = int(len(merged_df) * (1 - holdout_fraction))
    cutoff_date = merged_df.iloc[cutoff_idx]["Timestamp"]

    train_df = merged_df[merged_df["Timestamp"] <= cutoff_date]
    test_df = merged_df[merged_df["Timestamp"] > cutoff_date]

    train_journeys = build_journeys(train_df)
    test_journeys = build_journeys(test_df)

    train_weights, _, _ = compute_removal_effects(train_journeys)
    test_weights, _, _ = compute_removal_effects(test_journeys) if test_journeys else ({}, 0, 0)

    channels = sorted(set(train_weights) | set(test_weights))
    rows = [{
        "Channel": ch,
        "Train_Attribution_%": round(train_weights.get(ch, 0.0), 2),
        "Holdout_Attribution_%": round(test_weights.get(ch, 0.0), 2),
    } for ch in channels]
    validation_df = pd.DataFrame(rows).sort_values("Train_Attribution_%", ascending=False)

    # Spearman rank correlation (via ranked Pearson) — a single honest number
    # for "does the model's channel ranking hold up out-of-sample"
    if len(validation_df) > 1:
        train_ranks = validation_df["Train_Attribution_%"].rank(ascending=False)
        holdout_ranks = validation_df["Holdout_Attribution_%"].rank(ascending=False)
        rank_correlation = float(np.corrcoef(train_ranks, holdout_ranks)[0, 1])
    else:
        rank_correlation = float("nan")

    return validation_df, rank_correlation


# ---------------------------------------------------------------------------
# STEP 5 — CPA + FATIGUE-ADJUSTED BUDGET REALLOCATION (parameterized exponent)
# ---------------------------------------------------------------------------
def compute_cpa_and_budget(attribution_weights, base_conversions, spend_df, fatigue_exponent=0.5):
    historical_spend = spend_df.groupby("Channel")["Total_Budget_Allocated"].sum()

    rows = []
    for channel, weight in attribution_weights.items():
        conversions = base_conversions * (weight / 100)
        spend = historical_spend.get(channel, 0)
        cpa = spend / conversions if conversions > 0 else np.nan
        rows.append({"Channel": channel, "Historical_Spend": spend,
                      "True_Conversions": conversions, "True_CPA": cpa})

    cpa_df = pd.DataFrame(rows).set_index("Channel")
    cpa_df["Efficiency_Score"] = 1 / cpa_df["True_CPA"]
    cpa_df["Fatigue_Adjusted_Score"] = cpa_df["Efficiency_Score"] ** fatigue_exponent
    total_score = cpa_df["Fatigue_Adjusted_Score"].sum()
    cpa_df["Optimized_Allocation_%"] = (cpa_df["Fatigue_Adjusted_Score"] / total_score) * 100
    cpa_df["Recommended_Budget_INR"] = (cpa_df["Optimized_Allocation_%"] / 100) * BUDGET_PER_BRAND
    return cpa_df.sort_values("Optimized_Allocation_%", ascending=False)


def sweep_fatigue_exponent(attribution_weights, base_conversions, spend_df,
                            exponents=FATIGUE_EXPONENTS_TO_SWEEP):
    """One row per (exponent, channel) — drop straight into Power BI as a
    'What-If Parameter' table so a stakeholder can drag a slider and watch
    the budget split update live."""
    rows = []
    for exp in exponents:
        cpa_df = compute_cpa_and_budget(attribution_weights, base_conversions, spend_df,
                                         fatigue_exponent=exp)
        for channel, row in cpa_df.iterrows():
            rows.append({
                "Fatigue_Exponent": exp,
                "Channel": channel,
                "Optimized_Allocation_%": round(row["Optimized_Allocation_%"], 2),
                "Recommended_Budget_INR": round(row["Recommended_Budget_INR"], 0),
            })
    return pd.DataFrame(rows)



# ---------------------------------------------------------------------------
# PER-BRAND PIPELINE
# ---------------------------------------------------------------------------
def run_brand_analysis(brand_id, merged_df_brand, spend_df_brand, output_dir):
    os.makedirs(f"{output_dir}/{brand_id}", exist_ok=True)

    journeys = build_journeys(merged_df_brand)
    attribution_weights, base_conversions, base_rate = compute_removal_effects(journeys)

    naive_weights = compute_naive_last_click(journeys)
    comparison_df = build_naive_vs_true_comparison(attribution_weights, naive_weights)
    comparison_df.to_csv(f"{output_dir}/{brand_id}/naive_vs_true_attribution.csv", index=False)

    ci_df = bootstrap_attribution_ci(journeys, n_bootstrap=N_BOOTSTRAP)
    ci_df.to_csv(f"{output_dir}/{brand_id}/attribution_with_confidence_intervals.csv", index=False)

    validation_df, rank_correlation = time_based_holdout(merged_df_brand)
    validation_df.to_csv(f"{output_dir}/{brand_id}/holdout_validation.csv", index=False)

    brand_budget = spend_df_brand["Total_Budget_Allocated"].sum()
    cpa_df = compute_cpa_and_budget(attribution_weights, base_conversions, spend_df_brand)
    # Recompute recommended budget against this brand's ACTUAL total, not the
    # global constant, since B05 runs over the standard ₹10Cr
    cpa_df["Recommended_Budget_INR"] = (cpa_df["Optimized_Allocation_%"] / 100) * brand_budget
    cpa_df.round(2).to_csv(f"{output_dir}/{brand_id}/final_budget_allocation.csv")

    return {
        "brand_id": brand_id,
        "n_users": merged_df_brand["User_ID"].nunique(),
        "n_conversions": base_conversions,
        "conversion_rate_%": round(base_rate * 100, 2),
        "rank_correlation": round(rank_correlation, 3) if pd.notna(rank_correlation) else None,
        "brand_budget_INR": brand_budget,
        "cpa_df": cpa_df,
        "comparison_df": comparison_df,
    }


def run_pipeline(data_dir=DATA_DIR, output_dir=OUTPUT_DIR):
    os.makedirs(output_dir, exist_ok=True)

    touchpoints_df = pd.read_csv(f"{data_dir}/touchpoints.csv", parse_dates=["Timestamp"])
    profiles_df = pd.read_csv(f"{data_dir}/user_profiles.csv")
    spend_df = pd.read_csv(f"{data_dir}/campaign_spend.csv")
    spend_df["Total_Budget_Allocated"] = pd.to_numeric(spend_df["Total_Budget_Allocated"], errors="coerce")

    print("Step 1: SQL-based ETL + bot filtering (SQLite, window functions + CTEs)...")
    print("        (run across all brands together; bot behavior isn't brand-specific")
    print("         and users never overlap between brands)")
    merged_df, bot_summary, journey_paths, bot_by_brand = build_sql_pipeline(touchpoints_df, profiles_df)
    bot_summary.to_csv(f"{output_dir}/bot_funnel_summary.csv", index=False)
    bot_by_brand.to_csv(f"{output_dir}/bot_summary_by_brand.csv", index=False)
    journey_paths.to_csv(f"{output_dir}/user_journey_paths.csv", index=False)
    print(f"  -> {bot_summary.iloc[0]['total_users']} users, "
          f"{bot_summary.iloc[0]['volume_bot_users'] + bot_summary.iloc[0]['speed_bot_users']} bot flags")

    merged_df["Brand_ID"] = merged_df["User_ID"].str.extract(r"U_(B\d+)_")
    brands = sorted(merged_df["Brand_ID"].dropna().unique())
    print(f"\nDetected {len(brands)} brands in this dataset: {brands}")
    print("Running attribution + budget reallocation SEPARATELY per brand")
    print("(pooling brands together would blend unrelated campaigns' spend/CPA)\n")

    portfolio_rows = []
    all_comparisons = []
    for brand_id in brands:
        print(f"  -> {brand_id}...")
        brand_df = merged_df[merged_df["Brand_ID"] == brand_id]
        brand_spend = spend_df[spend_df["Brand_ID"] == brand_id]
        result = run_brand_analysis(brand_id, brand_df, brand_spend, output_dir)
        portfolio_rows.append({
            "Brand_ID": brand_id,
            "Users": result["n_users"],
            "Conversions": result["n_conversions"],
            "Conversion_Rate_%": result["conversion_rate_%"],
            "Holdout_Rank_Correlation": result["rank_correlation"],
            "Budget_INR": round(result["brand_budget_INR"], 0),
            "Top_Channel_By_Allocation": result["cpa_df"]["Optimized_Allocation_%"].idxmax(),
        })
        cdf = result["comparison_df"].copy()
        cdf["Brand_ID"] = brand_id
        all_comparisons.append(cdf)

    portfolio_summary = pd.DataFrame(portfolio_rows)
    portfolio_summary.to_csv(f"{output_dir}/portfolio_summary_by_brand.csv", index=False)

    all_comparisons_df = pd.concat(all_comparisons, ignore_index=True)
    portfolio_channel_view = all_comparisons_df.groupby("Channel").agg(
        Avg_Naive_LastClick_pct=("Naive_LastClick_%", "mean"),
        Avg_True_Attribution_pct=("True_Markov_Attribution_%", "mean"),
        Avg_Relative_Difference_pct=("Relative_Difference_%", "mean"),
        Brands_Covered=("Brand_ID", "nunique"),
    ).round(2).sort_values("Avg_True_Attribution_pct", ascending=False)
    portfolio_channel_view.to_csv(f"{output_dir}/portfolio_channel_view.csv")

    print(f"\nPer-brand exports written to ./{output_dir}/<BRAND_ID>/")
    print(f"Portfolio rollups written to ./{output_dir}/portfolio_summary_by_brand.csv")
    print(f"                          and ./{output_dir}/portfolio_channel_view.csv\n")
    print("--- Portfolio summary by brand ---")
    print(portfolio_summary.to_string(index=False))
    print("\n--- Portfolio-level channel view (averaged across brands) ---")
    print(portfolio_channel_view.to_string())

    return {
        "portfolio_summary": portfolio_summary,
        "portfolio_channel_view": portfolio_channel_view,
        "bot_summary": bot_summary,
    }


if __name__ == "__main__":
    run_pipeline()