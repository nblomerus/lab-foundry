"""
Schemas for the experiments agent.

Two LLM steps bookend a sandboxed code run:
  - `experiments.design`    → ExperimentDesign  (a complete, self-contained script)
  - `experiments.interpret` → ExperimentReport  (honest read of the numbers)

The design's `code` is what the sandbox runs (see agents/experiments/sandbox.py):
it may import ONLY the preinstalled stack, has NO network and NO file access
outside its cwd, and prints exactly ONE JSON object as its final stdout line =
the result. The report turns that result into a first-person lab note and a
bounded confidence nudge for the direction under test.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ExperimentDesign(BaseModel):
    """One small, reproducible experiment the sandbox can run end-to-end."""

    hypothesis: str = Field(..., description="The single thing this experiment tests, stated so a result can move it.")
    code: str = Field(
        ...,
        description=(
            "A complete, self-contained Python script. Imports ONLY the preinstalled stack "
            "(numpy / scipy / pandas / scikit-learn / xgboost / statsmodels / torch). Runs with NO "
            "network and NO file access outside its cwd — synthesize data or use sklearn/torch toy "
            "datasets. Prints exactly ONE JSON object as its final stdout line; that line is the result."
        ),
    )
    requires_gpu: bool = Field(default=False, description="True only if the experiment genuinely needs a GPU.")
    gpu_mem_mb: int | None = Field(default=None, description="GPU memory to request, if requires_gpu.")
    est_wall_clock_s: int = Field(default=600, ge=1, le=1800, description="Estimated wall-clock budget (cap 1800s).")
    est_mem_mb: int = Field(default=2048, ge=128, description="Estimated memory budget in MB.")
    seed: int = Field(default=0, description="RNG seed for reproducibility (set every library's seed in the code).")
    dataset_plan: str = Field(default="", description="How the script obtains its data (synthesize / toy dataset).")


class ExperimentReport(BaseModel):
    """The researcher's honest read of one completed experiment."""

    summary: str = Field(..., description="2-4 sentences. What was observed — name the actual numbers, plainly.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="How load-bearing this result is for the direction.")
    narrative_note: str = Field(
        ...,
        description=(
            "First-person lab note: what was hypothesized, what was run, what was observed, any "
            "surprises, what it means for the direction, and the suggested next step."
        ),
    )
    supports_direction: bool | None = Field(
        default=None,
        description="True if the result backs the direction, False if it pushes against it, null if neutral.",
    )
    confidence_delta: float = Field(
        default=0.0,
        ge=-0.3,
        le=0.3,
        description="How to move the direction's confidence (−0.3..+0.3). 0 when the result is neutral/inconclusive.",
    )
