# Executive Report: MCP Stress Traffic Impact on Harmonise Backend

## 1. Executive Summary

**The backend shows major instability under the observed traffic pattern, especially in tail latency and error bursts. The biggest risk is not average response time, but unpredictable slow responses and backend error spikes.**

Across **17,868 requests**, the system recorded:

| Metric                           |        Result | Executive Meaning                                                          |
| -------------------------------- | ------------: | -------------------------------------------------------------------------- |
| Total requests                   |        17,868 | Reasonable sample size for an initial stress review                        |
| Test sessions detected           |            33 | Tests were fragmented across many runs                                     |
| Active test duration             |    ~7.8 hours | Idle gaps were removed, which improves analysis quality                    |
| Removed idle time                |     ~87 hours | Important: raw wall-clock plots would have been misleading                 |
| Overall error rate               |         4.10% | Too high for production-facing backend traffic                             |
| Overall p95 latency              |  34.3 seconds | Slowest 5% of requests are far beyond acceptable UX/API levels             |
| Overall p99 latency              |  84.1 seconds | Worst 1% indicates serious backend queuing or timeout risk                 |
| Max observed latency             | 133.8 seconds | Some requests took over 2 minutes                                          |
| Latency spikes                   |         1,380 | Roughly 77 spikes per 1,000 requests                                       |
| Max observed request rate        |       4.4 RPS | Traffic volume is not extremely high, so backend sensitivity is concerning |
| Max estimated in-flight requests |            38 | Concurrency appears to amplify latency, but is not the only cause          |

> To prove causality, we still need side-by-side testing of direct Harmonise traffic versus MCP-mediated traffic, with backend server and database telemetry.

---

# 2. Overall Assessment

## Purpose – Result – Evaluation

| Purpose                                                                     | Result                                                                                                    | Evaluation                                                                                                                                                                             |
| --------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Understand whether MCP stress traffic affects Harmonise backend performance | Overall p95 latency is **34.3s**, p99 latency is **84.1s**, and worst case is **133.8s**                  | This is a material reliability concern. Even if most requests succeed, the user/business experience becomes unpredictable because a meaningful share of requests become extremely slow |
| Understand whether errors increase during traffic pressure                  | Overall error rate is **4.10%**                                                                           | This is too high for a production stock server. A 4% error rate can cause repeated retries, which may create a feedback loop and further increase backend load                         |
| Identify the main risk area                                                 | `GET api/v1/products/{skuCode}` accounts for most requests and most latency spikes                        | SKU-level product lookup is the main operational risk. It is likely the endpoint most exposed by MCP workflows because MCP may repeatedly resolve individual products/SKUs             |
| Identify whether request rate alone explains the issue                      | Severe latency appears even at low request rates; higher request rates are not always worse               | This suggests the issue is not purely “too many requests.” Backend state, cold starts, database performance, caching, connection limits, or earlier session conditions may be involved |
| Determine if the benchmark proves MCP is the root cause                     | The notebook measures traffic and backend responses, but does not compare MCP versus non-MCP direct calls | The current evidence is strong enough to justify risk controls, but not enough to assign root cause fully to MCP                                                                       |

---

# 3. Key Finding 1: The Backend Has Severe Tail-Latency Risk

The most important business finding is the difference between average behaviour and worst-user behaviour.

The average response time does not tell the full story. The p95 and p99 metrics show that the slowest group of requests becomes extremely slow:

| Metric                   |        Result |
| ------------------------ | ------------: |
| Overall p95 latency      |  34.3 seconds |
| Overall p99 latency      |  84.1 seconds |
| Maximum observed latency | 133.8 seconds |

## Evaluation

This means that although many requests may look acceptable, a significant minority become operationally unacceptable. For a production stock/product server, **30–80 seconds is not just “slow”; it can break downstream workflows**, especially if users or MCP agents are waiting for stock availability, dimensions, colours, or quote-related product enrichment.

This also creates a hidden risk: slow successful requests may still return HTTP 200, meaning they are technically “successful” but operationally poor. The notebook shows several very slow requests that still returned 200. From a business perspective, these should be treated as degraded service, not healthy service.

---

# 4. Key Finding 2: The SKU Lookup Endpoint Is the Main Latency Contributor

Endpoint-level results show a clear split.

| Endpoint                        | Requests | Error Rate |   p95 |   p99 |    Max | Spikes |
| ------------------------------- | -------: | ---------: | ----: | ----: | -----: | -----: |
| `GET api/v1/products/{skuCode}` |   14,037 |      1.82% | 40.0s | 90.9s | 133.8s |  1,318 |
| `GET api/v1/products`           |    3,831 |     12.45% | 30.2s | 30.3s |  30.8s |     62 |

## Purpose – Result – Evaluation

| Purpose                                                              | Result                                                                      | Evaluation                                                                                                                                     |
| -------------------------------------------------------------------- | --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| Identify which endpoint creates the largest backend latency exposure | SKU lookup has **p95 = 40s** and **p99 = 90.9s**                            | This endpoint is the main tail-latency driver. It should be prioritised for optimisation, caching, request batching, and database/query review |
| Identify which endpoint creates the largest error exposure           | Product-list endpoint has **12.45% error rate**                             | The product-list endpoint appears more error-prone, even though SKU lookup is slower overall                                                   |
| Understand MCP impact pattern                                        | MCP-style workflows often perform many SKU-level lookups to enrich products | If MCP expands each user question into multiple SKU lookups, it can amplify backend load even when user-facing traffic looks small             |

## Executive Interpretation

The backend has two different problems:

1. **SKU lookup is slow and unstable.**
2. **Product list endpoint has a high error rate.**

That matters because MCP may use both together: first search/list products, then perform multiple detailed SKU lookups. This can turn one user question into many backend calls.

---

# 5. Key Finding 3: The “Baseline vs Stress” Result Needs Careful Interpretation

The notebook reports:

| Metric                           | Result |
| -------------------------------- | -----: |
| Baseline p95                     |  76.8s |
| Stress p95                       |  27.2s |
| Stress vs baseline amplification |  0.35x |

At face value, this looks like stress traffic was faster than baseline. That is not a valid executive conclusion.

## Evaluation

The notebook labels traffic phases automatically based on observed request rate, not based on controlled test scenario names. Therefore, “baseline” does not necessarily mean a clean, healthy, low-load baseline. It includes early sessions where the backend was already performing very poorly.

This is one of the most important critical notes: **the phase labels are useful for rough exploration, but they should not be used as final proof that stress improves or reduces latency.**

The result suggests that some low-traffic periods were already unhealthy. That points to backend instability, cold starts, database contention, cache state, or previous-test residue.

---

# 6. Key Finding 4: The Saturation Curve Is Useful but Not Yet Conclusive

The notebook estimates an approximate saturation point at:

| Metric                      |   Result |
| --------------------------- | -------: |
| Approximate saturation knee | 0.30 RPS |
| Max observed RPS            | 4.40 RPS |

However, the saturation curve is not clean. Some low-RPS bands show worse latency than higher-RPS bands.

| RPS Band    |       p50 |       p95 | Error Rate |
| ----------- | --------: | --------: | ---------: |
| 0.2 RPS     |     23.2s |     29.2s |      8.35% |
| 0.3 RPS     |     30.2s |     45.8s |     16.92% |
| 0.4 RPS     |     18.8s |     30.3s |     29.62% |
| 0.6–1.0 RPS | ~2.1–3.2s | ~2.1–3.9s |      lower |
| 1.1 RPS     |      3.0s |     10.3s |      0.57% |
| 1.6 RPS     |      1.9s |     16.1s |      0.39% |

## Purpose – Result – Evaluation

| Purpose                                               | Result                                                     | Evaluation                                                                                                                                  |
| ----------------------------------------------------- | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| Find the backend’s capacity limit                     | Notebook estimates a knee around **0.30 RPS**              | This estimate is probably unstable because low-RPS periods include unhealthy sessions                                                       |
| Check whether latency increases smoothly with traffic | It does not. Higher RPS is sometimes faster than lower RPS | The backend issue is not purely traffic volume. Test-session conditions, caching, database state, or server warm-up likely affected results |
| Determine if production capacity can be declared      | Not yet                                                    | A cleaner controlled ramp test is needed before declaring a reliable production capacity threshold                                          |

## Executive Interpretation

The saturation curve is valuable as an early warning, but it should not be used yet as the official capacity number. The better conclusion is:

**The backend can show severe degradation even at modest traffic levels, and the current benchmark design does not yet isolate the exact traffic threshold.**

---

# 7. Diagram-by-Diagram Evaluation

## 7.1 Stress Timeline: Tail Latency vs Request Pressure

### Purpose

Show how latency changes over active benchmark time after removing idle gaps.

### Result

The timeline shows very large latency waves early in the benchmark, with p95/p99 rising toward **80–120 seconds**. Later periods have lower latency but still show repeated spikes.

### Evaluation

This is one of the strongest visuals in the notebook. It demonstrates that the backend experienced sustained periods of severe degradation, not just isolated outliers. However, it also shows that request pressure alone does not explain all latency because some severe periods occur at relatively low request rates.

---

## 7.2 Latency Acceleration Under Stress

### Purpose

Identify moments where latency starts worsening rapidly.

### Result

The largest acceleration periods occur early in the active timeline, especially around the first major degradation window. Smaller acceleration events also appear later.

### Evaluation

This is useful for identifying when the backend transitions from normal to degraded. For C-level reporting, this should be simplified as: **“The backend does not degrade gradually; it can tip into poor performance quickly.”**

---

## 7.3 Saturation Curve

### Purpose

Estimate the request rate where additional load creates disproportionate delay.

### Result

The notebook estimates the knee around **0.30 RPS**, but the curve is non-linear and inconsistent.

### Evaluation

The figure is helpful but should be labelled as “exploratory.” It should not be used as a final capacity claim because the traffic phases were not controlled and some low-RPS sessions were already unhealthy.

---

## 7.4 Endpoint p95 Heatmap

### Purpose

Compare endpoint behaviour across traffic phases.

### Result

The SKU endpoint has the worst p95 latency, especially in baseline and extreme stress. Product-list endpoint shows high latency in baseline/ramp but improves in later phases.

### Evaluation

This visual supports the conclusion that SKU lookup is the critical endpoint for latency risk. It should be included in the executive pack because it clearly shows which API needs priority attention.

---

## 7.5 Tail Latency vs Estimated In-Flight Concurrency

### Purpose

Understand whether concurrent requests are driving slow responses.

### Result

Latency generally rises during periods of higher in-flight requests, with maximum estimated in-flight concurrency around **38**.

### Evaluation

Concurrency appears to amplify the issue, but it is not the only cause. Some severe latency happens before the highest concurrency points. This suggests backend/database limits, queueing, cold starts, or connection-pool limits may also be involved.

---

# 8. Critical Notes on the Notebook Methodology

These points should be included in the report so leadership does not overinterpret the numbers.

## 8.1 The benchmark is useful, but not yet a clean capacity test

The notebook combines 33 sessions across multiple days. That is good for observing real instability, but it makes it harder to isolate the exact breaking point.

**Recommendation:** create a cleaner test matrix: warm baseline, single-endpoint ramp, mixed production ramp, burst test, soak test, and recovery test.

---

## 8.2 “Baseline” is not a true baseline

The notebook assigns baseline/ramp/stress based on observed RPS percentiles. This is useful when no scenario metadata exists, but it means “baseline” is not a controlled low-load state.

**Recommendation:** future data should include scenario name, target RPS, target concurrency, user count, test tool config, and MCP version.

---

## 8.3 RPS labelling needs correction

The notebook calculates RPS as requests per second, but one chart labels the axis as “Requests per 10 seconds.” That can confuse the reader.

**Recommendation:** relabel chart axes consistently as either “Requests per second” or “Requests per 10-second bucket.”

---

## 8.4 Small samples can distort p95 and p99

Some sessions have very few requests. Percentiles from small samples can look precise but are not reliable.

**Recommendation:** in executive tables, suppress p95/p99 for very small sample sessions or mark them as low-confidence.

---

# 9. Business Risk Assessment

| Risk                         | Severity    | Why It Matters                                                                                                       |
| ---------------------------- | ----------- | -------------------------------------------------------------------------------------------------------------------- |
| Slow product/SKU lookup      | High        | MCP agents may need to resolve many SKUs per user query; one user action can become many backend calls               |
| Product-list error rate      | High        | Failed list/search calls can break product discovery and trigger retries                                             |
| Long successful requests     | High        | HTTP 200 responses are not enough if users wait 30–100 seconds                                                       |
| Retry amplification          | High        | Errors and timeouts may cause clients/MCP to retry, increasing backend pressure                                      |
| Production testing risk      | High        | Running stress traffic against a shared production/stock server can affect real users                                |
| Unclear root cause           | Medium–High | Without backend/database telemetry, engineering may optimise the wrong layer                                         |
| Incomplete endpoint coverage | Medium      | Only two endpoints were analysed; booking, categories, departments, and other inventory flows may behave differently |

---

# 10. Recommended Executive Actions

## Immediate Guardrails

1. **Rate-limit MCP calls to Harmonise**, especially SKU-level calls.
2. **Add caching for product catalogue and SKU details**, with sensible expiry.
3. **Add request coalescing**, so repeated identical SKU requests are served once and shared.
4. **Add circuit-breaker behaviour**, so MCP stops hammering the backend when errors or latency rise.
5. **Set a timeout policy**, because waiting 100+ seconds is not operationally useful.
6. **Avoid uncontrolled stress tests on production/shared stock servers.**

## Short-Term Engineering Investigation

| Area                | What to Check                                                          |
| ------------------- | ---------------------------------------------------------------------- |
| Database            | Slow queries, locks, missing indexes, connection pool exhaustion       |
| Backend API         | Thread pool, request queue, memory, CPU, cold starts, timeout settings |
| Network/API Gateway | Gateway timeout, retry policies, request throttling                    |
| MCP Server          | Fan-out pattern, duplicate calls, retry behaviour, cache hit rate      |
| Data Shape          | Whether certain SKUs/products are much more expensive to fetch         |
| Error Codes         | Break down 500s by endpoint, session, and time window                  |

---

# Conclusion

The current benchmark shows that the Harmonise stock/product backend is vulnerable to severe tail-latency and error spikes under MCP-driven traffic patterns. The most affected area is SKU-level product lookup, which recorded p95 latency around 40 seconds, p99 around 91 seconds, and individual requests above two minutes. The product-list endpoint also showed a high error rate, above 12% overall.

The findings are strong enough to justify immediate traffic guardrails before broader MCP rollout. However, the current notebook should be treated as an exploratory stress analysis rather than a final capacity certification. The benchmark does not yet fully isolate whether the root cause sits in MCP fan-out, Harmonise API implementation, database performance, infrastructure limits, or a combination of these.
