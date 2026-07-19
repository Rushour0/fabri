# Fabri GitHub Action

Add this workflow at `.github/workflows/fabri-code-review.yml`:

```yaml
name: Fabri code review
on: pull_request
permissions:
  contents: read
  pull-requests: write
jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # The cache keeps `.fabri/code-review.db` as cumulative per-repo memory.
      - uses: actions/cache@v4
        with:
          path: .fabri/
          key: code-review-mem-${{ github.repository }}
      - uses: Rushour0/fabri/examples/github-action@main
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

The action requires the `OPENAI_API_KEY` repository secret. It runs the
code-review crew and posts **one summary PR comment**, rather than inline
comments. Its default `fail-on: request_changes` makes the check fail when the
verified verdict requests changes; set `fail-on: comment` to fail for any
non-approval verdict.
