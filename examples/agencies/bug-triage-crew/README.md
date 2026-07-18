# Bug triage-fix-test crew

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

```bash
export OPENAI_API_KEY=...          # a funded key
fabri serve --config examples/agencies/bug-triage-crew/agent.openai.yaml
# then, in examples/studio: npm run dev  ->  open http://localhost:5173
```

In Fabri Studio, submit a task like:

> `test_store.py is failing — the cart discount math looks wrong. Triage it, fix store.py with a minimal change, and verify the tests pass.`

Switch to the **Company** tab to watch the crew: triager → fixer → tester light
up as each finishes, with real per-agent COGS in the payroll counter.

## Re-running

The fixer edits `workspace/store.py`, so after a successful run the bug is gone.
Reset it before running again:

```bash
git checkout examples/agencies/bug-triage-crew/workspace/store.py
```
