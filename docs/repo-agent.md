# Repo agent

`fabri repo suggest-prompt` turns an agent's promoted memory guidelines into a
deduplicated issue. `fabri repo open-pr` goes one step further: it minimally
edits the raw `agent.yaml` text in a temporary clone, adding (or replacing) a
clearly marked learned-guidelines block in `system_prompt`, then opens or
updates a draft PR. The local working tree is never changed.

By default the provider is detected from CI, otherwise GitHub is used. Pass
`--provider github|gitlab|bitbucket` to choose explicitly. Tokens may be passed
with `--token` or supplied as `GITHUB_TOKEN`, `GITLAB_TOKEN`, or
`BITBUCKET_TOKEN`. GitLab requires a token with API and repository-write access;
Bitbucket accepts a repository access token (Bearer) or an app-password token
for Git HTTPS push.

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

To submit the actual prompt change for review instead, grant `contents: write`
and `pull-requests: write`, then run:

```sh
fabri repo open-pr --config agent.yaml --base main --branch fabri/self-improve
```

For a general tracking issue, use `fabri repo issue --title "..." --body
"..." --key stable-key`. Reusing a key updates the existing open issue instead
of creating a duplicate.
