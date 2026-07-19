# Repo agent

`fabri repo suggest-prompt` turns an agent's promoted memory guidelines into a
deduplicated GitHub issue. It uses `GITHUB_TOKEN` and, in GitHub Actions, the
automatically supplied `GITHUB_REPOSITORY` (`owner/name`). The workflow token
needs `issues: write` permission.

```yaml
name: Improve agent prompt

on:
  workflow_dispatch:

permissions:
  contents: read
  issues: write

jobs:
  improve:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install "fabri[sqlite]"
      - run: fabri run "<your agency task>" --config agent.yaml
      - run: fabri repo suggest-prompt --config agent.yaml
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

For a general tracking issue, use `fabri repo issue --title "..." --body
"..." --key stable-key`. Reusing a key updates the existing open issue instead
of creating a duplicate.
