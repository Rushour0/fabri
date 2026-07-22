# Scorer-agreement labeling

Build a blind sheet and keep its JSON key away from the human rater:

```bash
python benchmarks/agreement/build_label_sheet.py \
  --logs-root benchmarks/results/run-logs-2026-07-22 \
  --sample-size 36 --seed 20260722 \
  --out-sheet benchmarks/agreement/label_sheet.md \
  --out-key benchmarks/agreement/label_sheet_key.json
```

Enter exactly `pass` or `fail` plus a one-line reason for every item, then score it:

```bash
python benchmarks/agreement/score_agreement.py \
  --sheet benchmarks/agreement/label_sheet.md \
  --key benchmarks/agreement/label_sheet_key.json \
  --json-out benchmarks/agreement/agreement_results.json
```

The generated sheet, hidden key, and results are study artifacts; they are not committed.
