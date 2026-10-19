# Jev decision benchmark report

Results dir: `/Users/rushour0/playbook/fabri-playbook/bench/run2-keepalive-2026-10-19`. Rows: 1260. Arms: jev, gpt5mini, cascade.

## Accuracy, latency and cost by arm and family

Accuracy has a 95% Wilson interval. Latency is in milliseconds (nearest-rank percentiles). Cost per 1k correct divides total spend by correct decisions only.

| arm | family | n | accuracy | 95% CI | errors | p50 | p95 | p99 | cost / 1k | cost / 1k correct | escalation |
|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|
| jev | routing | 120 | 100.0% | 96.9 to 100.0 | 0 | 472 | 984 | 1501 | $0.0241 | $0.0241 | n/a |
| jev | tool_select | 120 | 100.0% | 96.9 to 100.0 | 0 | 471 | 654 | 3889 | $0.0219 | $0.0219 | n/a |
| jev | escalate_yesno | 100 | 95.0% | 88.8 to 97.8 | 0 | 459 | 546 | 827 | $0.0169 | $0.0177 | n/a |
| jev | urgency_score | 80 | 100.0% | 95.4 to 100.0 | 0 | 497 | 692 | 2740 | $0.0195 | $0.0195 | n/a |
| jev | all | 420 | 98.8% | 97.2 to 99.5 | 0 | 472 | 740 | 1784 | $0.0209 | $0.0211 | n/a |
| gpt5mini | routing | 120 | 92.5% | 86.4 to 96.0 | 0 | 1651 | 2944 | 3865 | $0.2908 | $0.3144 | n/a |
| gpt5mini | tool_select | 120 | 96.7% | 91.7 to 98.7 | 0 | 1575 | 2382 | 3466 | $0.2627 | $0.2718 | n/a |
| gpt5mini | escalate_yesno | 100 | 100.0% | 96.3 to 100.0 | 0 | 1862 | 3568 | 5037 | $0.3505 | $0.3505 | n/a |
| gpt5mini | urgency_score | 80 | 100.0% | 95.4 to 100.0 | 0 | 1693 | 2698 | 4596 | $0.2863 | $0.2863 | n/a |
| gpt5mini | all | 420 | 96.9% | 94.8 to 98.2 | 0 | 1686 | 2902 | 4040 | $0.2962 | $0.3056 | n/a |
| cascade | routing | 120 | 97.5% | 92.9 to 99.1 | 0 | 500 | 1087 | 2760 | $0.0334 | $0.0342 | 2.5% |
| cascade | tool_select | 120 | 100.0% | 96.9 to 100.0 | 0 | 498 | 615 | 1313 | $0.0219 | $0.0219 | 0.0% |
| cascade | escalate_yesno | 100 | 100.0% | 96.3 to 100.0 | 0 | 513 | 3764 | 5302 | $0.1065 | $0.1065 | 14.0% |
| cascade | urgency_score | 80 | 100.0% | 95.4 to 100.0 | 0 | 505 | 618 | 2927 | $0.0223 | $0.0223 | 1.2% |
| cascade | all | 420 | 99.3% | 97.9 to 99.8 | 0 | 504 | 1395 | 4194 | $0.0454 | $0.0457 | 4.3% |

## Cascade spend by provider

The cascade spent $0.0088 on Jev attempts and $0.0103 on gpt-5-mini fallbacks. Jev's share of cascade spend is 45.9%. 0 rows had no cost split.

## Calibration

Calibration uses P, the probability the model gave its chosen answer (the largest probability, or max(p, 1 - p) for yesno), as the predicted chance of being right. ECE uses 10 equal-width bins of P. Coverage is the share of decisions with P at or above the threshold, and accuracy is measured on that share only. The raw confidence columns show the decider's normalized confidence, where 0 means a uniform guess, so they are not probabilities. Brier is for yesno items and uses the probability given to yes.

| arm | scope | n with P | ECE | mean P | mean raw conf | share raw conf >= 0.99 | acc at P >= 0.6 | cover at P >= 0.6 | acc at P >= 0.8 | cover at P >= 0.8 | acc at P >= 0.95 | cover at P >= 0.95 | Brier (yesno) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| jev | all | 420 | 0.032 | 0.961 | 0.933 | 57.9% | 99.0% | 97.9% | 100.0% | 95.2% | 100.0% | 81.0% | 0.040 |
| jev | routing | 120 | 0.023 | 0.977 | 0.973 | 75.0% | 100.0% | 97.5% | 100.0% | 97.5% | 100.0% | 90.8% | n/a |
| jev | tool_select | 120 | 0.006 | 0.994 | 0.993 | 89.2% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 96.7% | n/a |
| jev | escalate_yesno | 100 | 0.089 | 0.892 | 0.783 | 0.0% | 95.8% | 95.0% | 100.0% | 86.0% | 100.0% | 42.0% | 0.040 |
| jev | urgency_score | 80 | 0.027 | 0.973 | 0.970 | 57.5% | 100.0% | 98.8% | 100.0% | 96.2% | 100.0% | 91.2% | n/a |
| cascade | all | 402 | 0.024 | 0.976 | 0.960 | 59.5% | 100.0% | 100.0% | 100.0% | 99.3% | 100.0% | 84.3% | 0.007 |
| cascade | routing | 117 | 0.012 | 0.988 | 0.986 | 76.1% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 90.6% | n/a |
| cascade | tool_select | 120 | 0.006 | 0.994 | 0.994 | 89.2% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 96.7% | n/a |
| cascade | escalate_yesno | 86 | 0.069 | 0.931 | 0.862 | 0.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 48.8% | 0.007 |
| cascade | urgency_score | 79 | 0.022 | 0.978 | 0.976 | 54.4% | 100.0% | 100.0% | 100.0% | 96.2% | 100.0% | 94.9% | n/a |

## Accuracy by perturbation tag

Tags pool across families. The row "(no perturbation)" holds items with no perturbation tag, for comparison. `none_applies` marks tool_select items whose gold is `none`.

| arm | tag | n | accuracy | 95% CI |
|---|---|---:|---:|---|
| jev | (no perturbation) | 212 | 100.0% | 98.2 to 100.0 |
| jev | injected_text | 55 | 100.0% | 93.5 to 100.0 |
| jev | large_action_set | 25 | 100.0% | 86.7 to 100.0 |
| jev | near_valid_alternative | 73 | 93.2% | 84.9 to 97.0 |
| jev | none_applies | 36 | 100.0% | 90.4 to 100.0 |
| jev | option_order_shuffled | 104 | 100.0% | 96.4 to 100.0 |
| gpt5mini | (no perturbation) | 212 | 99.1% | 96.6 to 99.7 |
| gpt5mini | injected_text | 55 | 96.4% | 87.7 to 99.0 |
| gpt5mini | large_action_set | 25 | 92.0% | 75.0 to 97.8 |
| gpt5mini | near_valid_alternative | 73 | 94.5% | 86.7 to 97.8 |
| gpt5mini | none_applies | 36 | 88.9% | 74.7 to 95.6 |
| gpt5mini | option_order_shuffled | 104 | 94.2% | 88.0 to 97.3 |
| cascade | (no perturbation) | 212 | 100.0% | 98.2 to 100.0 |
| cascade | injected_text | 55 | 100.0% | 93.5 to 100.0 |
| cascade | large_action_set | 25 | 96.0% | 80.5 to 99.3 |
| cascade | near_valid_alternative | 73 | 100.0% | 95.0 to 100.0 |
| cascade | none_applies | 36 | 100.0% | 90.4 to 100.0 |
| cascade | option_order_shuffled | 104 | 98.1% | 93.3 to 99.5 |

## Pre-registered pass rule

Jev-first cascade ships as the fabri routing default if, on routing and tool_select, its accuracy is within 2 points of gpt-5-mini or better, its p50 latency is at least 2x lower, and its cost per 1k correct decisions is lower. Act/no-act (escalate_yesno) decisions stay on the LLM or a human unless Jev's accuracy there is within 2 points of gpt-5-mini.

Accuracy within 2 points: yes. p50 at least 2x lower: yes. Cost per 1k correct lower: yes. Cascade ships as the routing default: yes.

Jev may take act/no-act decisions: no.
