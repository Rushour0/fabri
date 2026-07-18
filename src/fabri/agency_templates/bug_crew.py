"""Bug triage/fix/test crew template."""

from __future__ import annotations

FILES: dict[str, str] = {
    "agent.openai.yaml": '''agent:
  name: bug-triage-crew
  max_steps: 12
  subagent:
    max_steps: 6
  system_prompt: |
    You direct a fixed bug-fixing crew working on two files in the workspace:
    `store.py` (the code) and `test_store.py` (the tests, currently failing).

    Always work in this order, giving every specialist the file names:
      1. bug_triager  — localizes the defect and returns a diagnosis.
      2. bug_fixer    — applies the minimal fix from that diagnosis.
      3. bug_tester   — runs `python3 test_store.py` and reports PASS/FAIL.
    Finish only when bug_tester reports PASS. If it reports FAIL, send bug_fixer
    the exact failure and the diagnosis, then re-run bug_tester. Do not claim
    success unless the tester's own verdict is PASS. Keep the fix minimal —
    never edit the tests to make them pass.
  repair:
    enabled: true
    max_attempts: 1
    verify_command:
      - python3
      - __AGENCY_ROOT__/workspace/test_store.py
    repair_prompt: |
      The test suite still fails. Re-run bug_fixer on store.py to correct the
      cart_total discount logic (a 10% discount must keep 90% of the subtotal),
      then re-run bug_tester. Fix ONLY the code, never the tests:

      {errors}

llm:
  provider: openai
  model: gpt-5.6-terra
  max_tokens: 1024
  api_key_env: OPENAI_API_KEY

tools:
  manifest_dir: [builtin]
  enabled: [bug_triager, bug_fixer, bug_tester]
  sandbox_root: __AGENCY_ROOT__/workspace
  result_format: toon
  agents:
    - name: bug_triager
      description: Localize the failing bug and return a precise diagnosis (file, function, offending line, root cause). Read-only.
      config: __AGENCY_ROOT__/triager.openai.yaml
    - name: bug_fixer
      description: Apply the minimal code fix described in the diagnosis using edit_file. Never touches tests.
      config: __AGENCY_ROOT__/fixer.openai.yaml
    - name: bug_tester
      description: Run the test suite and return a concrete PASS/FAIL verdict. Read-only, no edits.
      config: __AGENCY_ROOT__/tester.openai.yaml

memory:
  backend: sqlite
  collection: bug_triage_crew_parent
  sqlite_path: .fabri/bug_triage_crew.db
  top_k: 3
  record_postmortems: true
''',
    "triager.openai.yaml": '''agent:
  name: bug-triager
  max_steps: 5
  system_prompt: |
    You are the triage specialist. A test is failing. Read the code and test
    files named in the task (use read_file; grep/list_dir if you must find
    them). Localize the defect precisely and return ONLY a diagnosis:
      - the file and function at fault,
      - the exact offending line/expression (quote it),
      - the root cause in one sentence,
      - the minimal correct behaviour (what the line SHOULD compute).
    Do NOT edit any files. Do NOT run anything. Keep it under 120 words.

llm:
  provider: openai
  model: gpt-5.6-luna
  max_tokens: 700
  api_key_env: OPENAI_API_KEY

tools:
  manifest_dir: [builtin]
  enabled: [read_file, grep, list_dir]
  sandbox_root: __AGENCY_ROOT__/workspace
  result_format: toon

memory:
  backend: sqlite
  collection: bug_triage_crew_triager
  sqlite_path: .fabri/bug_triage_crew.db
  top_k: 2
''',
    "fixer.openai.yaml": '''agent:
  name: bug-fixer
  max_steps: 6
  system_prompt: |
    You are the fix specialist. Apply the MINIMAL fix for the diagnosis in the
    task. First read_file the target to get the exact current text, then use
    edit_file to replace the single offending string with the corrected one.
    Change only what the diagnosis requires — do not reformat, rename, add
    features, or touch the tests. When done, state the one-line before → after
    change you made.

llm:
  provider: openai
  model: gpt-5.6-terra
  max_tokens: 900
  api_key_env: OPENAI_API_KEY

tools:
  manifest_dir: [builtin]
  enabled: [read_file, edit_file]
  sandbox_root: __AGENCY_ROOT__/workspace
  result_format: toon

memory:
  backend: sqlite
  collection: bug_triage_crew_fixer
  sqlite_path: .fabri/bug_triage_crew.db
  top_k: 2
''',
    "tester.openai.yaml": '''agent:
  name: bug-tester
  max_steps: 5
  system_prompt: |
    You are the test specialist. Run the test suite with bash exactly as:
      python3 test_store.py
    Report a clear verdict: PASS if it exits 0 and prints PASS, otherwise FAIL
    with the failing assertion message copied verbatim. Do NOT edit any code —
    you only run and report. If it FAILS, say what the fixer must revisit.

llm:
  provider: openai
  model: gpt-5.6-luna
  max_tokens: 700
  api_key_env: OPENAI_API_KEY

tools:
  manifest_dir: [builtin]
  enabled: [bash]
  sandbox_root: __AGENCY_ROOT__/workspace
  result_format: toon

memory:
  backend: sqlite
  collection: bug_triage_crew_tester
  sqlite_path: .fabri/bug_triage_crew.db
  top_k: 2
''',
    "workspace/store.py": '''"""A tiny store module. Ships with one deliberate pricing bug for the crew.

This is the fixture the bug-triage-crew agency operates on — the analogue of
the changelog agency's `release_input.json`. `test_store.py` pins the intended
behaviour and currently fails.
"""


def cart_total(items, discount=0.0):
    """Total price for a cart after an optional discount.

    ``items`` is a list of ``(unit_price, quantity)`` pairs. ``discount`` is a
    rate in [0, 1): ``0.1`` means 10% off.
    """
    subtotal = sum(price * qty for price, qty in items)
    return subtotal * discount  # apply the discount
''',
    "workspace/test_store.py": '''"""Tests that pin cart_total's intended behaviour. Self-locating so it runs
from any cwd (`python3 test_store.py`) — the tester agent and fabri's repair
verify_command both invoke it directly."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from store import cart_total


def test_applies_discount():
    # $10 x2 + $5 x1 = $25 subtotal; a 10% discount should leave $22.50.
    got = cart_total([(10.0, 2), (5.0, 1)], 0.1)
    assert got == 22.5, f"expected 22.5 after 10% discount, got {got}"


def test_no_discount_is_subtotal():
    assert cart_total([(3.0, 4)], 0.0) == 12.0


if __name__ == "__main__":
    test_applies_discount()
    test_no_discount_is_subtotal()
    print("PASS: cart_total handles discounts correctly")
''',
    "workspace/.gitignore": "__pycache__/\n*.pyc\n",
}

README = '''# Bug triage-fix-test crew

A three-specialist agency that fixes a real failing test end-to-end:

```text
orchestrator -> bug_triager -> bug_fixer -> bug_tester
                                  |              |
                                  +--- repair <--+
```

- **bug_triager** (read-only: `read_file`, `grep`, `list_dir`) localizes the
  defect and returns a diagnosis — file, function, offending line, root cause.
- **bug_fixer** (`read_file`, `edit_file`) applies the *minimal* code change.
- **bug_tester** (`bash`) runs `python3 test_store.py` and reports PASS/FAIL.

The manager runs them in order and only finishes when the tester reports PASS;
a failing run triggers fabri's repair loop (`verify_command` re-runs the test).
The specialists are sandboxed to `workspace/`, so they can only touch the
target — `store.py` (the code) and `test_store.py` (the tests).

## The fixture

`workspace/store.py` ships with one deliberate bug: `cart_total` multiplies the
subtotal by the discount rate instead of by `(1 - rate)`, so a 10% discount
returns 10% of the price. `workspace/test_store.py` pins the intended behaviour
and currently fails.

## Run it

Run fabri from `__RUN_FROM__`, because paths in the generated configs are
relative to that directory:

```bash
export OPENAI_API_KEY=...
fabri serve --config __AGENCY_ROOT__/agent.openai.yaml
fabri studio
```

Submit: `test_store.py is failing — triage it, minimally fix store.py, and
verify the tests pass.`

## Re-running

The fixer edits `workspace/store.py`, so after a successful run the bug is gone.
Restore `return subtotal * discount` before running the fixture again.
'''

TEMPLATE = {"FILES": FILES, "README": README}
