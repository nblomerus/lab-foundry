"""
chunker.py — Chunk parsed documents into retrieval-ready segments.

Vendored from rag-bench (`rag_bench/core/chunker.py`) for the Library's
Librarian (MIMIR_WARDEN_SCOPE.md §3, Phase 2). Self-contained and
deterministic: NO DB, NO LLM, NO network, NO third-party splitter dependency.

Handles AI/ML-specific challenges, preserved verbatim from rag-bench:
- Preserves mathematical equations as atomic units ($$...$$, \\[...\\],
  begin{equation|align|gather}) — never split mid-equation.
- Keeps table rows with their headers.
- Expands acronyms on first occurrence per chunk.
- Filters noisy sections (references, acknowledgments, ...) via SECTION_BLOCKLIST.
- Prepends a contextual prefix ("{title} — {Section}\\n\\n") for embedding quality.

The text-splitting logic is delegated to a pluggable ChunkingStrategy (see
`get_strategy`), so the algorithm stays swappable. Only the "recursive"
strategy is vendored. rag-bench's "semantic" strategy is intentionally NOT
vendored — it needs a sentence-transformer embedder, which would violate this
module's no-LLM / no-network contract. It remains a future option: register an
embedder-backed strategy in `STRATEGY_REGISTRY` when one is available.

Public surface adapted for the Librarian:
- `plan(doc: ParsedDoc) -> list[ChunkPlanItem]` is THE API the Librarian loop
  calls. It assigns a 0-based `ordinal` across the whole document, a sha256
  `content_hash`, and a cheap `token_count` estimate per chunk.
- `chunk_paper(doc)` is kept as the internal worker (mirrors rag-bench).
"""

from __future__ import annotations

import hashlib
import logging
import re

from labfoundry.research.librarian.schemas import ChunkPlanItem, ParsedDoc

log = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Constants copied from rag-bench's rag_bench/config.py (same values).
# -------------------------------------------------------------------------

MIN_CHUNK_LENGTH = 100  # skip chunks shorter than this (characters)

# Sections to exclude from indexing (noise that degrades retrieval at scale).
SECTION_BLOCKLIST = frozenset(
    {
        "references",
        "bibliography",
        "acknowledgments",
        "acknowledgements",
        "acknowledgment",
        "acknowledgement",
        "preamble",
        "author_contributions",
        "funding",
        "competing_interests",
        "data_availability",
        "ethics_statement",
    }
)


# -------------------------------------------------------------------------
# Equation + table detection patterns (verbatim from rag-bench).
# -------------------------------------------------------------------------

EQUATION_PATTERNS = [
    re.compile(r"\$\$.*?\$\$", re.DOTALL),  # $$...$$
    re.compile(r"\\\[.*?\\\]", re.DOTALL),  # \[...\]
    re.compile(r"\\begin\{equation\}.*?\\end\{equation\}", re.DOTALL),
    re.compile(r"\\begin\{align\}.*?\\end\{align\}", re.DOTALL),
    re.compile(r"\\begin\{gather\}.*?\\end\{gather\}", re.DOTALL),
]

TABLE_ROW_PATTERN = re.compile(r"^\|.*\|$", re.MULTILINE)
TABLE_SEPARATOR_PATTERN = re.compile(r"^\|[\s\-:]+\|$", re.MULTILINE)


# -------------------------------------------------------------------------
# format_authors — copied from rag-bench's rag_bench/utils/text.py.
# -------------------------------------------------------------------------


def format_authors(authors: list[str] | str, max_authors: int = 3) -> str:
    """Format author list for citation display."""
    if isinstance(authors, str):
        authors = [a.strip() for a in authors.split(",") if a.strip()]

    if not authors:
        return "Unknown"

    last_names = []
    for author in authors[:max_authors]:
        parts = author.strip().split()
        if parts:
            last_names.append(parts[-1])

    if len(authors) > max_authors:
        return f"{last_names[0]} et al."
    elif len(last_names) == 1:
        return last_names[0]
    elif len(last_names) == 2:
        return f"{last_names[0]} and {last_names[1]}"
    else:
        return ", ".join(last_names[:-1]) + f", and {last_names[-1]}"


# -------------------------------------------------------------------------
# RecursiveStrategy — vendored from rag-bench's strategies/recursive.py.
#
# rag-bench delegated the actual split to langchain's
# RecursiveCharacterTextSplitter. To keep this module dependency-free and
# deterministic, we vendor a faithful reimplementation of that algorithm:
# try a hierarchy of separators (paragraph > line > sentence > clause >
# phrase > word > char), recursing into oversized pieces, then merge adjacent
# splits up to chunk_size with chunk_overlap carry-over. Same separator order,
# same length_function=len semantics.
# -------------------------------------------------------------------------

# Same priority order as rag-bench's RecursiveStrategy.
_RECURSIVE_SEPARATORS = [
    "\n\n",  # paragraph break (highest priority)
    "\n",  # line break
    ". ",  # sentence break
    "; ",  # clause break
    ", ",  # phrase break
    " ",  # word break
    "",  # character break (last resort)
]


class RecursiveStrategy:
    """Split text using recursive character boundaries.

    Separators are tried in priority order — paragraph breaks first,
    character-level as a last resort. This keeps logical structure intact for
    most well-formatted text. Behavior mirrors langchain's
    RecursiveCharacterTextSplitter (the splitter rag-bench used).
    """

    def __init__(self, chunk_size: int = 1024, chunk_overlap: int = 128):
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive, got {chunk_size}")
        if chunk_overlap < 0:
            raise ValueError(f"chunk_overlap must be non-negative, got {chunk_overlap}")
        if chunk_overlap >= chunk_size:
            raise ValueError(f"chunk_overlap ({chunk_overlap}) must be less than chunk_size ({chunk_size})")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap

    def split_text(self, text: str) -> list[str]:
        return self._split(text, _RECURSIVE_SEPARATORS)

    # -- internals (langchain RecursiveCharacterTextSplitter semantics) ----

    def _split(self, text: str, separators: list[str]) -> list[str]:
        """Recursively split `text` using the highest-priority separator that
        appears in it, then merge the resulting splits back up to chunk_size."""
        final_chunks: list[str] = []

        # Pick the first separator that occurs in the text; "" is the fallback.
        separator = separators[-1]
        new_separators: list[str] = []
        for i, sep in enumerate(separators):
            if sep == "":
                separator = sep
                break
            if sep in text:
                separator = sep
                new_separators = separators[i + 1 :]
                break

        splits = self._split_on_separator(text, separator)

        # Re-split any piece still larger than chunk_size with finer separators;
        # accumulate small pieces to be merged.
        good_splits: list[str] = []
        merge_sep = "" if separator == "" else separator
        for s in splits:
            if len(s) < self.chunk_size:
                good_splits.append(s)
            else:
                if good_splits:
                    final_chunks.extend(self._merge_splits(good_splits, merge_sep))
                    good_splits = []
                if not new_separators:
                    final_chunks.append(s)
                else:
                    final_chunks.extend(self._split(s, new_separators))
        if good_splits:
            final_chunks.extend(self._merge_splits(good_splits, merge_sep))

        return [c for c in final_chunks if c]

    @staticmethod
    def _split_on_separator(text: str, separator: str) -> list[str]:
        """Split on `separator`, keeping the separator attached to each piece so
        merges can re-join without losing it (matches langchain keep_separator)."""
        if separator == "":
            return list(text)
        # Keep the separator at the end of each piece (except trailing empty).
        parts = text.split(separator)
        result: list[str] = []
        for i, part in enumerate(parts):
            if i < len(parts) - 1:
                result.append(part + separator)
            else:
                result.append(part)
        return [p for p in result if p != ""]

    def _merge_splits(self, splits: list[str], separator: str) -> list[str]:
        """Greedily merge consecutive splits into chunks <= chunk_size, then
        carry `chunk_overlap` characters of tail into the next chunk."""
        sep_len = len(separator)
        chunks: list[str] = []
        current: list[str] = []
        total = 0

        for s in splits:
            s_len = len(s)
            addition = s_len + (sep_len if current else 0)
            if total + addition > self.chunk_size and current:
                chunks.append(self._join(current, separator))
                # Drop from the front until we're under the overlap budget
                # (and under chunk_size for the next addition).
                while current and (total > self.chunk_overlap or (total + addition > self.chunk_size and total > 0)):
                    removed = current.pop(0)
                    total -= len(removed) + (sep_len if current else 0)
            current.append(s)
            total += s_len + (sep_len if len(current) > 1 else 0)

        if current:
            chunks.append(self._join(current, separator))
        return chunks

    @staticmethod
    def _join(pieces: list[str], separator: str) -> str:
        joined = separator.join(pieces)
        return joined.strip()


# Minimal strategy registry + factory, vendored from strategies/__init__.py.
# Only "recursive" is registered (semantic skipped — needs an embedder).
STRATEGY_REGISTRY: dict[str, type] = {
    "recursive": RecursiveStrategy,
}


def get_strategy(name: str, config: dict | None = None):
    """Instantiate a chunking strategy by name.

    Only "recursive" is available here. A "semantic" strategy (rag-bench has
    one) would need an embedder and is intentionally not vendored — register
    it in STRATEGY_REGISTRY when an offline embedder is wired up.
    """
    if name not in STRATEGY_REGISTRY:
        available = ", ".join(sorted(STRATEGY_REGISTRY))
        raise ValueError(f"Unknown chunking strategy '{name}'. Available: {available}")
    cls = STRATEGY_REGISTRY[name]
    return cls(**(config or {}))


# -------------------------------------------------------------------------
# PaperChunker — vendored + adapted from rag-bench.
# -------------------------------------------------------------------------


class PaperChunker:
    """
    Chunks documents with domain-aware splitting (AI/ML-tuned).

    Key features (preserved from rag-bench):
    - Equation-aware: never splits inside math blocks
    - Table-aware: keeps table rows with column headers
    - Acronym expansion: expands first occurrence per chunk
    - Section filtering: skips references, acknowledgments, and other noise
    - Contextual prefix: prepends doc title + section for embedding quality

    The Librarian calls `plan(doc)`; `chunk_paper(doc)` is the internal worker.
    """

    def __init__(
        self,
        chunk_size: int = 1024,
        chunk_overlap: int = 128,
        min_section_length: int = 50,
        *,
        strategy=None,
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_section_length = min_section_length

        if strategy is not None:
            self.strategy = strategy
        else:
            self.strategy = get_strategy(
                "recursive",
                {"chunk_size": chunk_size, "chunk_overlap": chunk_overlap},
            )

        self._equation_store: dict[str, str] = {}

    # -- public Librarian API -----------------------------------------------

    def plan(self, doc: ParsedDoc) -> list[ChunkPlanItem]:
        """Chunk `doc` and return `ChunkPlanItem` rows aligned to migration
        015's `chunks` table.

        `ordinal` is assigned 0-based across the WHOLE document (not per
        section), `content_hash` is sha256(text), and `token_count` is a cheap
        char/4 estimate (a real tokenizer can replace it later).
        """
        self._reset_equation_store()
        worker_chunks = self.chunk_paper(doc)

        items: list[ChunkPlanItem] = []
        for ordinal, ck in enumerate(worker_chunks):
            text = ck["text"]
            items.append(
                ChunkPlanItem(
                    ordinal=ordinal,
                    text=text,
                    content_hash=hashlib.sha256(text.encode()).hexdigest(),
                    token_count=len(text) // 4,  # cheap estimate; swap for a tokenizer later
                    section=ck["section"],
                )
            )
        return items

    # -- internal worker (mirrors rag-bench's chunk_paper) ------------------

    def chunk_paper(self, doc: ParsedDoc) -> list[dict]:
        """
        Chunk a parsed document into retrieval-ready segments.

        Returns a list of dicts: {"text", "section"} (plus citation metadata),
        in document order. `plan()` wraps these into ChunkPlanItem rows.
        """
        chunks: list[dict] = []
        acronyms = doc.acronyms or {}

        source_display = f'{format_authors(doc.authors)} ({doc.year}) "{doc.title}"'

        sections = doc.sections
        if not sections:
            # Fallback: chunk the full text as a single section.
            sections = {"full_text": doc.full_text or ""}

        for section_name, section_text in sections.items():
            # Skip noisy sections that degrade retrieval at scale.
            if section_name.lower() in SECTION_BLOCKLIST:
                continue

            if not section_text or len(section_text.strip()) < self.min_section_length:
                continue

            # Pre-process: protect equations from splitting, then tables.
            protected_text = self._protect_equations(section_text)
            protected_text = self._protect_tables(protected_text)

            # Split into chunks (delegated to the strategy).
            text_chunks = self.strategy.split_text(protected_text)

            for chunk_text in text_chunks:
                # Restore any equation placeholders.
                chunk_text = self._restore_equations(chunk_text)
                # Expand acronyms (first occurrence per chunk).
                chunk_text = self._expand_acronyms(chunk_text, acronyms)
                # Clean up whitespace.
                chunk_text = self._clean_text(chunk_text)

                if len(chunk_text.strip()) < MIN_CHUNK_LENGTH:
                    continue  # skip trivially small chunks

                # Prepend contextual prefix for better embedding quality.
                section_label = section_name.replace("_", " ").title()
                prefix = f"{doc.title} — {section_label}\n\n"
                chunk_text = prefix + chunk_text

                categories = doc.categories or []
                if isinstance(categories, list):
                    categories = ",".join(categories)

                chunks.append(
                    {
                        "text": chunk_text,
                        "section": section_name,
                        "metadata": {
                            "source_display": source_display,
                            "title": doc.title,
                            "year": doc.year,
                            "arxiv_id": doc.arxiv_id or "",
                            "section": section_name,
                            "topic": doc.topic or "",
                            "categories": categories,
                        },
                    }
                )

        return chunks

    # -- equation handling (verbatim from rag-bench) ------------------------

    def _protect_equations(self, text: str) -> str:
        """Replace equations with placeholders so the splitter doesn't break
        them. Stores equations in self._equation_store for later restoration."""
        for pattern in EQUATION_PATTERNS:
            for match in pattern.finditer(text):
                eq_id = f"__EQ_{len(self._equation_store):04d}__"
                self._equation_store[eq_id] = match.group(0)
                text = text.replace(match.group(0), eq_id, 1)
        return text

    def _restore_equations(self, text: str) -> str:
        """Restore equation placeholders back to original equations."""
        for eq_id, equation in self._equation_store.items():
            text = text.replace(eq_id, equation)
        return text

    def _reset_equation_store(self) -> None:
        """Clear the equation store between documents."""
        self._equation_store = {}

    # -- table handling (verbatim from rag-bench) ---------------------------

    def _protect_tables(self, text: str) -> str:
        """Ensure table rows stay with their column headers."""
        lines = text.split("\n")
        result_lines = []
        in_table = False

        for line in lines:
            is_table_row = bool(TABLE_ROW_PATTERN.match(line.strip()))
            is_separator = bool(TABLE_SEPARATOR_PATTERN.match(line.strip()))

            if is_table_row and not in_table:
                in_table = True
                result_lines.append(line)
            elif (is_separator and in_table) or (is_table_row and in_table):
                result_lines.append(line)
            else:
                if in_table:
                    in_table = False
                result_lines.append(line)

        return "\n".join(result_lines)

    # -- acronym expansion (verbatim from rag-bench) ------------------------

    def _expand_acronyms(self, text: str, acronyms: dict[str, str]) -> str:
        """Expand the first occurrence of each acronym in the chunk.

        Example: "MIPS" -> "Maximum Inner Product Search (MIPS)".
        Only expands a standalone word, and only if not already expanded.
        """
        if not acronyms:
            return text

        for acronym, full_form in acronyms.items():
            pattern = rf"\b{re.escape(acronym)}\b"
            already_expanded = f"{full_form} ({acronym})"
            if already_expanded not in text and re.search(pattern, text):
                text = re.sub(pattern, f"{full_form} ({acronym})", text, count=1)
        return text

    # -- text cleanup (verbatim from rag-bench) -----------------------------

    def _clean_text(self, text: str) -> str:
        """Clean up whitespace and formatting artifacts."""
        text = re.sub(r"\n{3,}", "\n\n", text)
        lines = [line.strip() for line in text.split("\n")]
        text = "\n".join(lines)
        return text.strip()
