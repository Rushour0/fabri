# Releasing fabri

fabri publishes to [PyPI](https://pypi.org/project/fabri/) via **Trusted
Publishing** (OIDC) — no API tokens are stored in the repo. The
`.github/workflows/release.yml` workflow runs on any `v*` tag.

## One-time setup (per project)

1. Sign in to https://pypi.org.
2. Go to **Your account → Publishing → Add a new pending publisher**.
3. Fill in:
   - **PyPI project name:** `fabri`
   - **Owner:** `Rushour0`
   - **Repository name:** `fabri`
   - **Workflow name:** `release.yml`
   - **Environment name:** `pypi`
4. Save. (This authorizes the GitHub Action to publish without a token. The
   project is created on first successful publish.)

Optionally do the same on https://test.pypi.org first to rehearse.

## Cut a release

1. Bump `version` in `pyproject.toml` (PyPI versions are immutable — never reuse one).
2. Commit, then tag and push:
   ```bash
   git commit -am "Release v0.1.0"
   git tag v0.1.0
   git push origin main v0.1.0
   ```
3. The `release` workflow builds the sdist + wheel and publishes to PyPI.
4. Verify: `pip install fabri==0.1.0` in a clean environment.

## Manual fallback (if not using CI)

```bash
python -m build                 # -> dist/*.whl + dist/*.tar.gz
twine check dist/*
twine upload dist/*             # prompts for a PyPI API token
```
