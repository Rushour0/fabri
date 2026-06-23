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
2. **Bump the BSL Change Date in `LICENSE`** to *release date + 4 years*
   (the maximum BSL 1.1 allows). The Change Date in `LICENSE` applies
   per-version, so each new release should reset it to a fresh 4-year
   window — otherwise later releases ship with a Change Date that's
   already partially elapsed, shortening the source-available period.
   - Edit the `Change Date:` line in `LICENSE` (e.g. cutting v0.7.7 on
     2026-08-10 → `Change Date: 2030-08-10`).
   - The `2030-06-23` date referenced in `README.md`, `COMMERCIAL.md`,
     and `CHANGELOG.md` prose is informational; update those to match
     if you want the docs to reflect the new ceiling, but the
     `LICENSE` value is the legally controlling one.
3. Commit, then tag and push:
   ```bash
   git commit -am "Release v0.1.0"
   git tag v0.1.0
   git push origin main v0.1.0
   ```
4. The `release` workflow builds the sdist + wheel and publishes to PyPI.
5. Verify: `pip install fabri==0.1.0` in a clean environment.

## Manual fallback (if not using CI)

```bash
python -m build                 # -> dist/*.whl + dist/*.tar.gz
twine check dist/*
twine upload dist/*             # prompts for a PyPI API token
```
