"""
The synthesis agent — the lab's terminal step.

Researchers and the experiments lane generate per-task findings and per-experiment
lab notes that move a direction's confidence. Nothing read ACROSS a direction's
experiments to assemble a defensible result, so the loop dead-ended at "confidence
moved + corpus grew". This agent closes it: when a direction has accumulated enough
completed experiments, it composes them into a paper-shaped FINDING (claim + method +
numbers + limitations + so-what), graduates the direction, and ingests the finding
into the Library so it compounds and feeds Ariadne's next deliberation.

The mode-dial agent name is `synthesis` (this module's path).
"""
