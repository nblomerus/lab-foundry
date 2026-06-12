"""Schemas for the research arc's written artifacts — shared by their two authors:
Ariadne (PI) writes the literature review + research proposal (agents/ariadne/
scholarship.py); Synthesis, the lab's writer of record, composes the article
(agents/synthesis/article.py)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LiteratureReview(BaseModel):
    """A corpus-grounded literature review for one direction."""

    title: str = Field(..., description="Review title, e.g. 'Self-consistency for small LLMs: what is known'.")
    body_md: str = Field(
        ...,
        min_length=600,
        description=(
            "Markdown with sections: ## Background, ## Key prior work (cite EXACT corpus titles), "
            "## What is established, ## Open gaps, ## Positioning (how THIS direction fills a gap)."
        ),
    )
    citations: list[str] = Field(
        ..., min_length=4, description="EXACT titles of corpus papers the review rests on (no inventions)."
    )


class Hypothesis(BaseModel):
    hid: str = Field(..., description="Short id, e.g. 'H1'.")
    statement: str = Field(..., description="Falsifiable claim with a measurable threshold.")
    metric: str = Field(..., description="The metric that decides it (e.g. 'GSM8K exact-match accuracy').")
    threshold: str = Field(..., description="The decision rule (e.g. '≥5 pp over greedy at n=5 samples').")
    dataset_plan: str = Field(default="", description="Data: /data benchmark slice, builtin, or synthesized.")


class ResearchProposal(BaseModel):
    """The formal proposal that precedes experimentation."""

    title: str
    research_questions: list[str] = Field(..., min_length=1, max_length=5)
    hypotheses: list[Hypothesis] = Field(..., min_length=1, max_length=6)
    method_plan: str = Field(
        ..., min_length=300, description="How the experiment series will test the hypotheses on the lab's hardware."
    )
    success_criteria: str = Field(..., description="What outcome concludes the direction (and what kills it).")


class Article(BaseModel):
    """The final write-up: a full article (concluded direction) or research note (mixed evidence)."""

    title: str
    abstract: str = Field(..., min_length=300)
    body_md: str = Field(
        ...,
        min_length=1500,
        description=(
            "Full IMRaD markdown: ## Introduction, ## Related work (from the literature review), "
            "## Method (the experiments actually run), ## Results (the REAL numbers), ## Discussion, "
            "## Limitations, ## Reproducibility (seeds, code hashes, image digests)."
        ),
    )
    citations: list[str] = Field(default_factory=list, description="EXACT corpus titles cited.")
