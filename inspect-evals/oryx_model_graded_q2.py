"""
Oryx model-graded Q2 scorer, built on Inspect AI (UK AI Security Institute, MIT).

GRANT: Amartya granted this on 28 Oct 2026 ("granted", after the Inspect
audit-log scorer was reported).
HELD, NOT RUN: the study-2 arm-2 regen retry is LIVE at the time this
file was written (13:37 IST cron, thrifty pass 1/3, s2-a2-02 regenerating).
Running a model-graded eval now would compete for the same model quota
and risk an ABORT-429 for that retry. Do NOT run this task until the
retry's report has landed and quota is clear.

What it will do when run:
  For each verified citation, a model grader judges whether the cited
  source Supports, Partially supports, or Does not support the claim —
  exactly Paper 2's Q2 labels. The grader is compared against the human
  Q2 label, so this measures grader agreement, NOT a new audit result.
  It must never replace the human Q2 census, and its output must never
  be reported as Paper 2 results.

Dataset: samples are loaded from q2_samples.jsonl in this directory,
  one JSON object per line: {"claim": ..., "source": ..., "target": ...}
  where target is one of Supports / Partially / Does not support.
  Built 28 Oct 2026 from human Q2 records only: labels from
  q2-claim-support.md, claims from q2_work/q2_claims.md (marker-carrying
  sentences only), source excerpts from q2_work/src_*.txt — 27 samples
  (Supports 6, Partially supports 13, Does not support 8; the 1
  Unassessable row excluded, as in Paper 2). No invented claims.

Run (only after the retry report, with a provider configured):
  inspect eval oryx_model_graded_q2.py --model <provider>/<model> --log-dir ./logs
  Provider choice is a separate decision — do not hard-code a paid
  provider here, and do not use a key that is not already authorised.
"""
import json
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.scorer import model_graded_qa
from inspect_ai.solver import generate

SAMPLES_FILE = Path(__file__).resolve().parent / "q2_samples.jsonl"

GRADE_INSTRUCTIONS = (
    "You are grading whether a cited source supports a claim, using "
    "Paper 2's Q2 labels. Read the claim and the source excerpt, then "
    "answer with exactly one line: GRADE: Supports, GRADE: Partially, "
    "or GRADE: Does not support. "
    "Supports = the source fully supports the claim as stated. "
    "Partially = the source partly supports it, or supports a weaker "
    "version, or is ambiguous. "
    "Does not support = the source contradicts the claim, does not "
    "address it, or the excerpt gives no basis for it."
)


def _load_samples():
    if not SAMPLES_FILE.exists():
        return []
    samples = []
    for line in SAMPLES_FILE.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        samples.append(
            Sample(
                input=f"Claim: {row['claim']}\nSource excerpt: {row['source']}",
                target=row["target"],
            )
        )
    return samples


@task
def oryx_model_graded_q2():
    return Task(
        dataset=MemoryDataset(_load_samples()),
        solver=generate(),
        scorer=model_graded_qa(
            instructions=GRADE_INSTRUCTIONS,
            grade_pattern=r"(?i)GRADE: (Supports|Partially|Does not support)",
        ),
    )
