"""
ops.mimir_ask — ask Mimir a multi-hop question over the Library (GraphRAG). Read-only.

    set -a; . ./.env; set +a
    python -m ops.mimir_ask "What methods quantify uncertainty in novel view synthesis?"

Hybrid retrieval finds the papers, the context graph reasons across them (multi-hop),
DeepSeek synthesises the answer + citations + GAPS. Needs DATABASE_URL + NEO4J + DEEPSEEK_API_KEY.
"""

from __future__ import annotations

import asyncio
import sys

from dotenv import load_dotenv

from agents.mimir.ask import answer_question


async def run(question: str) -> int:
    load_dotenv()
    print(f"Q: {question}\n" + "=" * 78)
    a = await answer_question(question)
    print("\nANSWER\n  " + a.answer.replace("\n", "\n  "))
    if a.citations:
        print("\nCITATIONS")
        for c in a.citations:
            print(f"  - {c}")
    if a.related_concepts:
        print("\nRELATED CONCEPTS (graph): " + ", ".join(a.related_concepts))
    if a.gaps:
        print("\nGAPS (thinly covered — request these next):")
        for g in a.gaps:
            print(f"  - {g}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print('usage: python -m ops.mimir_ask "<question>"', file=sys.stderr)
        return 2
    return asyncio.run(run(" ".join(sys.argv[1:])))


if __name__ == "__main__":
    sys.exit(main())
