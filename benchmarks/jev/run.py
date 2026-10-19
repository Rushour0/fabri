"""Run the Jev vs gpt-5-mini decision benchmark.

Examples::

    # offline check of the whole pipeline (no keys, no network)
    python benchmarks/jev/run.py --arms jev,gpt5mini,cascade --dry-run --limit 20 --out /tmp/jevdry

    # live run
    python benchmarks/jev/run.py --arms jev,gpt5mini,cascade --families all \\
        --env-file path/to/.env --out benchmarks/jev/results/2026-10-19T120000Z

Each decision is appended to ``<out>/results.jsonl`` as soon as it finishes, so a
crash or Ctrl-C keeps everything done so far. Re-running with the same ``--out``
skips (arm, id) pairs already in the file. ``fabri.decide`` is imported only for
live arms, so ``--dry-run`` works without it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import dataset  # noqa: E402

QUESTION_KEY = "q"
THRESHOLDS = {"choice": 0.6, "score": 0.6, "yesno": 0.6}
JEV_MODEL = "typesafe/jev-1.13"
LLM_CONFIG = {
    "provider": "openai",
    "model": "gpt-5-mini",
    "api_key_env": "PLAYBOOK_OPENAI_API_KEY",
    # gpt-5-mini spends hidden reasoning tokens from this budget; 400 can return an empty answer.
    "max_tokens": 2048,
}
ARM_KEYS = {
    "jev": ["OPENROUTER_API_KEY"],
    "gpt5mini": ["PLAYBOOK_OPENAI_API_KEY"],
    "cascade": ["OPENROUTER_API_KEY", "PLAYBOOK_OPENAI_API_KEY"],
}
ALL_ARMS = tuple(ARM_KEYS)


# ---------------------------------------------------------------- env file

def load_env_file(path: str | os.PathLike) -> list[str]:
    """Read simple KEY=VALUE lines into os.environ. Returns the key names only.

    Blank lines and ``#`` comments are skipped, a leading ``export`` is allowed,
    matching single or double quotes around a value are removed, and an unquoted
    `` #`` starts a trailing comment. Variables already set in the environment win.
    Values are never printed or returned.
    """
    names = []
    text = Path(path).read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)
        names.append(key)
    return names


# ---------------------------------------------------------------- helpers

def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("yes", "true", "y", "1"):
            return True
        if v in ("no", "false", "n", "0"):
            return False
    return None


def is_correct(kind: str, value, gold) -> bool:
    if kind == "yesno":
        v = to_bool(value)
        return v is not None and v == gold
    return isinstance(value, str) and value.strip().lower() == str(gold).lower()


def _item_key(state, question) -> str:
    blob = json.dumps({"state": state, "question": question}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


class RateLimiter:
    """Spaces out call starts for one arm to at most ``rps`` per second (0 = off)."""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps if rps and rps > 0 else 0.0
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_at)
            self.next_at = start + self.interval
        delay = start - time.monotonic()
        if delay > 0:
            time.sleep(delay)


# ---------------------------------------------------------------- fake deciders (dry run)

class FakeAnswer:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeDecider:
    """Returns the gold answer after a random short sleep. Numbers are synthetic."""

    COST = {"jev": 0.00001, "gpt5mini": 0.0001}
    LATENCY_MS = {"jev": (2, 12), "gpt5mini": (10, 40)}

    def __init__(self, name: str, gold_by_key: dict, seed: int):
        self.name = name
        self.gold_by_key = gold_by_key
        self.seed = seed
        self.model = f"fake-{name}"

    def decide(self, state, questions):
        out = {}
        for q in questions:
            qd = {"kind": _get(q, "kind"), "instructions": _get(q, "instructions"),
                  "options": _get(q, "options"), "levels": _get(q, "levels")}
            qd = {k: v for k, v in qd.items() if v is not None}
            key = _item_key(state, qd)
            rng = random.Random(f"{self.seed}:{self.name}:{key}")
            lo, hi = self.LATENCY_MS[self.name]
            latency = rng.uniform(lo, hi)
            time.sleep(latency / 1000.0)
            value = self.gold_by_key[key]
            confidence = round(rng.uniform(0.3, 1.0), 3)
            if qd["kind"] == "yesno":
                # confidence = |2p - 1|, so the chosen side has p = (1 + confidence) / 2
                p_chosen = (1.0 + confidence) / 2.0
                probabilities = {"true": p_chosen if value else 1.0 - p_chosen,
                                 "false": 1.0 - p_chosen if value else p_chosen}
            else:
                probabilities = {str(value): round(rng.uniform(max(0.5, confidence), 1.0), 3)}
            out[_get(q, "key")] = FakeAnswer(
                key=_get(q, "key"), kind=qd["kind"], value=value,
                probabilities=probabilities, confidence=confidence,
                source=self.name, latency_ms=round(latency, 3), input_tokens=100,
                output_tokens=5, cost_usd=self.COST[self.name], model=self.model,
                escalated=False, error=None,
            )
        return out


class FakeCascade:
    """Mirrors the cascade contract: ask primary, escalate below threshold."""

    def __init__(self, primary, fallback, thresholds):
        self.primary, self.fallback, self.thresholds = primary, fallback, thresholds

    def decide(self, state, questions):
        first = self.primary.decide(state, questions)
        out = {}
        for q in questions:
            k = _get(q, "key")
            a = first[k]
            conf = _get(a, "confidence")
            if conf is None or conf < self.thresholds.get(_get(q, "kind"), 0.6):
                b = self.fallback.decide(state, [q])[k]
                out[k] = FakeAnswer(**{
                    **b.__dict__, "escalated": True, "source": "fallback",
                    "latency_ms": (a.latency_ms or 0) + (b.latency_ms or 0),
                    "cost_usd": (a.cost_usd or 0) + (b.cost_usd or 0),
                })
            else:
                out[k] = FakeAnswer(**{**a.__dict__, "source": "primary"})
        return out


def answer_cost(answer) -> float | None:
    """An answer's cost in USD, or None when it is unknown (no cost reported,
    or the decider could not price the model and set cost_known False)."""
    if answer is None or _get(answer, "cost_known", True) is False:
        return None
    return _num(_get(answer, "cost_usd"))


class Recorder:
    """Wraps one decider and notes the cost of every Answer it returns during one
    item. Costs are copied at return time, so a caller that later edits the
    Answer object in place cannot change what was recorded. An unpriced answer
    is recorded as None."""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0
        self.costs = []

    def decide(self, state, questions):
        self.calls += 1
        result = self._inner.decide(state, questions)
        try:
            self.costs.extend(answer_cost(a) for a in result.values())
        except AttributeError:
            pass
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _cost_of(rec: "Recorder") -> float | None:
    """0.0 when the decider was never called. None when it was called but
    reported no cost, or any answer's cost is unknown."""
    if not rec.calls:
        return 0.0
    if not rec.costs or any(c is None for c in rec.costs):
        return None
    return sum(rec.costs)


def _sum_costs(values) -> float | None:
    values = list(values)
    return None if any(v is None for v in values) else sum(values)


# ---------------------------------------------------------------- arms

class Arm:
    """Builds the decider for one item. Cascade gets fresh recorders per item so
    the Jev attempt and the fallback cost are split exactly."""

    def __init__(self, name: str, dry_run: bool, gold_by_key: dict, seed: int):
        self.name = name
        self.dry_run = dry_run
        if dry_run:
            self._jev = FakeDecider("jev", gold_by_key, seed)
            self._llm = FakeDecider("gpt5mini", gold_by_key, seed)
            self._cascade_cls = FakeCascade
        else:
            from fabri.decide import CascadeDecider, JevDecider, LLMDecider  # lazy: live only

            self._jev = JevDecider(model=JEV_MODEL, provider="openrouter") if name in ("jev", "cascade") else None
            self._llm = LLMDecider(dict(LLM_CONFIG)) if name in ("gpt5mini", "cascade") else None
            self._cascade_cls = CascadeDecider

    def decider(self):
        if self.name == "jev":
            return self._jev, None
        if self.name == "gpt5mini":
            return self._llm, None
        primary, fallback = Recorder(self._jev), Recorder(self._llm)
        cascade = self._cascade_cls(primary=primary, fallback=fallback, thresholds=dict(THRESHOLDS))
        return cascade, (primary, fallback)


def make_question(item: dict, dry_run: bool):
    q = item["question"]
    kw = dict(key=QUESTION_KEY, kind=q["kind"], instructions=q["instructions"],
              options=q.get("options"), levels=q.get("levels"))
    if dry_run:
        return FakeAnswer(**kw)
    from fabri.decide import Question  # lazy: live only

    return Question(**kw)


# ---------------------------------------------------------------- runner

def run_one(arm: Arm, item: dict, limiter: RateLimiter, dry_run: bool, retry_delay: float) -> dict:
    question = make_question(item, dry_run)
    kind = item["question"]["kind"]
    answer, error, attempts, parts = None, None, 0, None
    tries = []  # recorder pairs from every cascade attempt, so a failed try's spend still counts
    attempt_costs = []  # spend of each attempt (None = unknown), summed when the item was retried
    wall_ms = None
    for attempt in range(2):  # first try plus one retry
        attempts = attempt + 1
        decider, parts = arm.decider()
        single = None
        if parts:
            tries.append(parts)
        else:
            decider = single = Recorder(decider)
        limiter.wait()
        t0 = time.perf_counter()
        try:
            result = decider.decide(item["state"], [question])
            answer = (result or {}).get(QUESTION_KEY)
            error = _get(answer, "error") if answer is not None else "no answer returned for question key"
        except Exception as exc:  # recorded in the row, then retried once
            answer, error = None, f"{type(exc).__name__}: {exc}"
        wall_ms = (time.perf_counter() - t0) * 1000.0
        if parts:
            attempt_costs.append(_sum_costs([_cost_of(parts[0]), _cost_of(parts[1])]))
        else:
            attempt_costs.append(_cost_of(single))
        if not error:
            break
        if attempt == 0:
            time.sleep(retry_delay)

    value = _get(answer, "value")
    latency = _num(_get(answer, "latency_ms"))
    row = {
        "arm": arm.name,
        "id": item["id"],
        "family": item["family"],
        "kind": kind,
        "tags": item["tags"],
        "gold": item["gold"],
        "value": value,
        "correct": bool(answer is not None and not error and is_correct(kind, value, item["gold"])),
        "confidence": _num(_get(answer, "confidence")),
        "probabilities": _get(answer, "probabilities"),
        "latency_ms": latency if latency is not None else round(wall_ms, 3),
        "wall_ms": round(wall_ms, 3),
        "cost_usd": answer_cost(answer),
        "input_tokens": _get(answer, "input_tokens"),
        "output_tokens": _get(answer, "output_tokens"),
        "model": _get(answer, "model"),
        "source": _get(answer, "source"),
        "escalated": _get(answer, "escalated"),
        "error": error,
        "attempts": attempts,
        "dry_run": dry_run,
    }
    if arm.name == "cascade":
        primary, fallback = parts
        row["jev_cost_usd"] = _sum_costs(_cost_of(p) for p, _ in tries)
        row["fallback_cost_usd"] = _sum_costs(_cost_of(f) for _, f in tries)
        if row["cost_usd"] is None and row["jev_cost_usd"] is not None:
            row["cost_usd"] = row["jev_cost_usd"] + (row["fallback_cost_usd"] or 0.0)
        if row["escalated"] is None:
            row["escalated"] = bool(fallback.calls)
    if attempts > 1:
        # A retried item cost every attempt, not just the last one. For the
        # cascade this equals jev_cost_usd + fallback_cost_usd.
        row["cost_usd"] = _sum_costs(attempt_costs)
    return row


def _done_pairs(path: Path) -> set[tuple[str, str]]:
    done = set()
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn last line from a crash; the pair is re-run
                done.add((r.get("arm"), r.get("id")))
    return done


def _append(path: Path, lock: threading.Lock, record: dict):
    line = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    with lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()


def select_items(families: str, limit: int | None) -> list[dict]:
    fams = dataset.FAMILIES if families == "all" else [f.strip() for f in families.split(",") if f.strip()]
    items = []
    for fam in fams:
        fam_items = dataset.load([fam])
        items.extend(fam_items[:limit] if limit else fam_items)
    return items


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--arms", default="jev,gpt5mini,cascade", help="comma list of: " + ", ".join(ALL_ARMS))
    p.add_argument("--families", default="all", help="'all' or a comma list of: " + ", ".join(dataset.FAMILIES))
    p.add_argument("--limit", type=int, default=None, help="first N items per family")
    p.add_argument("--out", default=None, help="results dir (default benchmarks/jev/results/<UTC timestamp>)")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--rps", type=float, default=4.0, help="max call starts per second per arm (0 = no limit)")
    p.add_argument("--retry-delay", type=float, default=2.0, help="seconds before the single retry")
    p.add_argument("--dry-run", action="store_true", help="fake deciders that return gold; no network")
    p.add_argument("--seed", type=int, default=7, help="seed for dry-run latency and confidence")
    p.add_argument("--env-file", default=None, help="KEY=VALUE file to load (values are never printed)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    bad = [a for a in arms if a not in ARM_KEYS]
    if bad:
        print(f"unknown arm(s): {', '.join(bad)}; expected {', '.join(ALL_ARMS)}", file=sys.stderr)
        return 2

    if args.env_file:
        try:
            names = load_env_file(args.env_file)
        except OSError as exc:
            print(f"could not read env file {args.env_file}: {type(exc).__name__}", file=sys.stderr)
            return 2
        needed = sorted({k for a in arms for k in ARM_KEYS[a]})
        found = ", ".join(f"{k}={'set' if os.environ.get(k) else 'missing'}" for k in needed)
        print(f"env file: read {len(names)} keys (values not shown). {found}")

    skipped = {}
    if not args.dry_run:
        for a in list(arms):
            missing = [k for k in ARM_KEYS[a] if not os.environ.get(k)]
            if missing:
                skipped[a] = missing
                print(f"skipping arm {a}: missing env var {', '.join(missing)}")
                arms.remove(a)
        if not arms:
            print("no arms left to run", file=sys.stderr)
            return 1
        try:
            import fabri.decide  # noqa: F401
        except ImportError as exc:
            print(f"live run needs fabri.decide, which failed to import: {exc}", file=sys.stderr)
            return 2

    out = Path(args.out) if args.out else HERE / "results" / datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True)
    results = out / "results.jsonl"
    log = out / "run_log.jsonl"
    lock = threading.Lock()

    items = select_items(args.families, args.limit)
    gold_by_key = {_item_key(it["state"], it["question"]): it["gold"] for it in items}
    built = {a: Arm(a, args.dry_run, gold_by_key, args.seed) for a in arms}
    limiters = {a: RateLimiter(0 if args.dry_run else args.rps) for a in arms}

    done = _done_pairs(results)
    jobs = [(a, it) for it in items for a in arms if (a, it["id"]) not in done]
    _append(log, lock, {
        "event": "start", "at": datetime.now(timezone.utc).isoformat(), "dry_run": args.dry_run,
        "arms": arms, "skipped_arms": skipped, "families": args.families, "limit": args.limit,
        "concurrency": args.concurrency, "rps": args.rps, "items": len(items),
        "jobs": len(jobs), "already_done": len(done), "jev_model": JEV_MODEL,
        "llm_model": LLM_CONFIG["model"], "thresholds": THRESHOLDS,
    })
    print(f"{len(items)} items x {len(arms)} arms; {len(jobs)} to run, {len(done)} already in {results}")

    n_ok = n_err = 0
    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = {pool.submit(run_one, built[a], it, limiters[a], args.dry_run, args.retry_delay): (a, it) for a, it in jobs}
        for fut in as_completed(futs):
            a, it = futs[fut]
            try:
                row = fut.result()
            except Exception as exc:  # should not happen; run_one records errors itself
                row = {"arm": a, "id": it["id"], "family": it["family"], "kind": it["question"]["kind"],
                       "tags": it["tags"], "gold": it["gold"], "value": None, "correct": False,
                       "confidence": None, "latency_ms": None, "cost_usd": None, "escalated": None,
                       "error": f"runner: {type(exc).__name__}: {exc}", "attempts": 0, "dry_run": args.dry_run}
            _append(results, lock, row)
            if row["error"]:
                n_err += 1
            else:
                n_ok += 1
    _append(log, lock, {"event": "end", "at": datetime.now(timezone.utc).isoformat(), "ok": n_ok, "errors": n_err})
    print(f"done: {n_ok} ok, {n_err} errors. rows in {results}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
