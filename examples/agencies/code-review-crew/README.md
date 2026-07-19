# Code-review crew

This read-only fabri agency reviews a unified pull-request diff with two
specialists: `code_reviewer` proposes concrete findings and `review_verifier`
removes false positives before returning a structured verdict. Its SQLite memory
is stored at `.fabri/code-review.db`, so caching `.fabri/` preserves per-repo
review context between CI runs.

## Run locally

```bash
fabri --config examples/agencies/code-review-crew/agent.openai.yaml run "Review the unified diff in workspace/pr.diff. Return the JSON verdict."
```

The included `workspace/pr.diff` is a fixture with a deliberately introduced
pagination boundary bug for local review experimentation.
