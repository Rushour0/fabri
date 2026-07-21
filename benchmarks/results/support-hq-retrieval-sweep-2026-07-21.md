# Retrieval-configuration sweep — Support HQ memory study

**Question.** The memory-vs-control study found that trace-backed memory does not improve holdout
reliability on Support HQ (memory 7/10 vs control 9/10 at ten replicas). Does varying the *retrieval
configuration* — how much memory is pulled (`top_k`) or how it is selected (`retrieval_strategy`) —
recover a benefit?

**Method.** Re-ran the memory arm with the compiled retrieval config overridden (via the memory
runner's `--retrieval-top-k` / `--retrieval-strategy`), against the same trained memory:

| Retrieval config | Guidelines retrieved (memory) | Guidelines retrieved (control) |
|---|---:|---:|
| `top_k=5`, `hybrid` (baseline) | **2** (every run, 10/10) | 0 |
| `top_k=10`, `hybrid` | **2** (every run) | 0 |
| `hybrid+mmr` | **2** (every run) | 0 |

**Finding — retrieval configuration is not the lever here.** Raising `top_k` from 5 to 10, and
switching to MMR-diversified selection, changed *nothing* about what was retrieved: memory returned the
same **2** guidelines every run in all three configurations. The reason is upstream — **the trained
memory only contains ~2 guidelines** (that is how many the training run mines). You cannot retrieve
more than exists, so a retrieval knob that raises the ceiling above the store's size is a no-op by
construction. Because the agent receives the identical 2 guidelines regardless of retrieval config, the
holdout outcome cannot differ from the baseline (no benefit).

**Implication.** The bottleneck to making self-improving memory *pay* is not retrieval breadth or
strategy — it is **guideline supply and quality**: how many good, distinct lessons a training run mines,
and how they are promoted and pruned. That is the next experiment worth running, not further retrieval
tuning.

**Honesty / provenance.** The retrieval-count invariance above is deterministic and was observed on
every completed pair. A full ten-replica *rubric* re-measurement for each variant was not completed —
repeated infrastructure interruptions cut the runs short (8 memory / 7 control completed pairs per
variant before interruption). That does not affect the conclusion: the knob demonstrably does not
change the retrieved evidence, so it cannot change the outcome. The applied override was verified to
write `memory.top_k` / `memory.retrieval_strategy` into the compiled config before each run. Fabri
v0.18.5; setup qualification and memory-effect are separate claims.
