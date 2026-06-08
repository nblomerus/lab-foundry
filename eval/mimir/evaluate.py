"""
Mimir trust-gate evaluation — runs the frozen gold set (eval/mimir/cases.py)
through `library.trust.classify_trust` and reports accuracy + the safety metrics
that actually matter for "no unverified source enters the Library".

Pure + offline (classify_trust takes no DB / network), so this is deterministic and
needs nothing running. A mismatch is a real finding (bug or policy gap).

    python -m eval.mimir.evaluate          # report
    python -m eval.mimir.evaluate --strict # exit 1 on ANY mismatch (CI)

Safety metrics:
  * FALSE-ADMIT  — a case that should be quarantined/blocked was admitted. The
                   worst failure: a bad source entering the trusted corpus.
  * SPOOF-LEAK   — a lookalike/adversarial source reached a TRUSTED tier.
  * OVER-BLOCK   — a legitimate source was spuriously quarantined (availability).
"""

from __future__ import annotations

import argparse
import logging

from eval.mimir.cases import CASES, TRUSTED_TIERS
from library.trust import DocMeta, classify_trust

log = logging.getLogger("eval.mimir")


def _evaluate() -> dict:
    rows = []
    for case in CASES:
        tc = classify_trust(DocMeta(**case["meta"]))
        got = {"tier": tc.tier, "blocked": tc.blocked, "needs_llm": tc.needs_llm}
        exp = case["expect"]
        ok = got == exp
        # Did the expectation call for non-admission (quarantine/block)?
        exp_blocked = exp["blocked"] or exp["tier"] == "quarantined"
        got_blocked = got["blocked"] or got["tier"] == "quarantined"
        false_admit = exp_blocked and not got_blocked
        spoof_leak = case["cat"] == "spoof" and exp["tier"] not in TRUSTED_TIERS and got["tier"] in TRUSTED_TIERS
        over_block = (not exp_blocked) and got_blocked
        rows.append(
            {
                "case": case,
                "got": got,
                "exp": exp,
                "ok": ok,
                "false_admit": false_admit,
                "spoof_leak": spoof_leak,
                "over_block": over_block,
            }
        )
    return {"rows": rows}


def _report(res: dict) -> int:
    rows = res["rows"]
    n = len(rows)
    correct = sum(r["ok"] for r in rows)
    mismatches = [r for r in rows if not r["ok"]]
    false_admits = [r for r in rows if r["false_admit"]]
    spoof_leaks = [r for r in rows if r["spoof_leak"]]
    over_blocks = [r for r in rows if r["over_block"]]

    # per-category accuracy
    cats: dict[str, list[bool]] = {}
    for r in rows:
        cats.setdefault(r["case"]["cat"], []).append(r["ok"])

    print("\n" + "=" * 76)
    print("MIMIR TRUST-GATE EVAL — classify_trust over the frozen gold set")
    print("=" * 76)
    print(f"  overall accuracy : {correct}/{n} = {correct / n:.3f}")
    print("  by category      : " + "  ".join(f"{c}={sum(v)}/{len(v)}" for c, v in sorted(cats.items())))
    print("-" * 76)
    print("  SAFETY (the metrics that matter):")
    print(f"    FALSE-ADMIT (bad source admitted) : {len(false_admits)}   <- must be 0")
    print(f"    SPOOF-LEAK  (lookalike -> trusted): {len(spoof_leaks)}   <- must be 0")
    print(f"    OVER-BLOCK  (legit quarantined)   : {len(over_blocks)}")
    print("=" * 76)

    if mismatches:
        print(f"\nMISMATCHES ({len(mismatches)}) — each is a bug or a policy gap:")
        for r in mismatches:
            c = r["case"]
            flags = " ".join(
                f
                for f, on in (
                    ("FALSE-ADMIT", r["false_admit"]),
                    ("SPOOF-LEAK", r["spoof_leak"]),
                    ("OVER-BLOCK", r["over_block"]),
                )
                if on
            )
            print(f"  [{c['cat']}/{c['id']}] {flags}")
            print(f"     expect {r['exp']}")
            print(f"     got    {r['got']}")
            print(f"     why    {c['why']}")
    else:
        print("\nAll cases match the intended policy. The deterministic gate is")
        print("provably correct on every tier, every hard-gate, and every spoof here.")
    print()
    # exit code: any safety failure is always fatal; --strict makes any mismatch fatal.
    return 1 if (false_admits or spoof_leaks) else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(prog="eval.mimir.evaluate")
    ap.add_argument("--strict", action="store_true", help="exit 1 on ANY mismatch (not just safety failures)")
    args = ap.parse_args()
    res = _evaluate()
    code = _report(res)
    if args.strict and any(not r["ok"] for r in res["rows"]):
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
