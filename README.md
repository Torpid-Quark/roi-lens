# ROI Lens: Multi-Touch Attribution & Budget Reallocation Engine
**Author** Harsh Patil   

##  Executive Summary
Nexus Brands was utilizing a heuristic "Last-Click" as their only marketing attribution model, that resulted in heavily skewed Cost Per Acquisition (CPA) metrics and inefficient budget allocation. 

**ROI Lens** is a probabilistic machine learning pipeline that reads raw clickstream data, purges non human bot traffic and maps the customer journey using **Markov Chains**. By calculating the "Removal Effect" of each channel, the model exposes the true Cost Per Acquisitions and automatically reallocates a ₹10 Crore budget using an ad-fatigue dampener to maximize the conversion volume

##  Architecture
1. **Data Pipeline & Anomaly Detection:** - Processed +500,000 raw touchpoints
   - Engineered logic to identify and drop of +11,500 bot interactions considering physical limitations thresholds (sub-1 second click intervals and anomalous volume spikes)
2. **Markov Chain Attribution:**
   - Mapped chronological user journeys to calculate the Transition Matrix.
   - Utilized Linear Algebra and fundamental matrix equation to simulate Removal Effect uncovering the hidden value of top funnel channels (Influencers, Instagram)
3. **Fatigue-Adjusted Engine:**
   - Recalculated the True CPA for all marketing channels
   - Engineered a square-root mathematical dampener to safely scale high performing channels without triggering audience saturation or ad fatigue

##  Business Impact
* Proved that historical metrics were undervaluing Influencer networks by over 40%
* Identified YouTube as a severe cost-sink (True CPA: ₹283k vs Influencer CPA: ₹115k)
* Delivered a mathematically optimized ₹10 Crore reallocation strategy to drastically lower the blended CPA for the upcoming quarter

##  Tech Stack
* **Language:** Python
* **Libraries:** Pandas, NumPy, Collections
* **Concepts:** Linear Algebra, Markov Chains, Probability Matrices, Data Cleaning