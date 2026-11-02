"""
Oryx audit-log scorer, built on Inspect AI (UK AI Security Institute, MIT).

This is NOT a model evaluation. It does not generate anything and does not
call any model, API, or network. It takes an already-finished Oryx AI job's
mechanical verification record and scores it through Inspect's task/log
machinery, so Oryx audit results can be stored, compared and re-run in
the same format as any other Inspect eval.

Sample: MVP verification job 20261027-234737-ac7df9
  ("AI adoption in Indian healthcare, 2024-2026", gemini-3.1-flash-lite)
  verification record: run/06-verification.json
  markers 13, sources cited 3, invalid markers [], claim sentences 36,
  cited claim sentences 13, sourced-claim rate 36%, status PASS.

Run (offline, no model):  inspect eval oryx_audit_check.py --model mockllm/model
The mock model is never called: the solver echoes the recorded audit
record instead of generating. If Inspect ever requires a real model
here, stop — this task must stay generation-free.
"""
import json
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import ModelOutput
from inspect_ai.scorer import Score, Scorer, accuracy, scorer
from inspect_ai.solver import Generate, Solver, solver

JOB_DIR = Path(__file__).resolve().parent.parent / "pilot-jobs" / "20261027-234737-ac7df9" / "run"
VERIFICATION = json.loads((JOB_DIR / "06-verification.json").read_text())


@scorer(metrics=[accuracy()])
def audit_scorer() -> Scorer:
    async def score(state, target):
        # state.output is the recorded verification record, echoed by the solver.
        record = json.loads(state.output.completion)
        ok = (
            record["status"] == "PASS"
            and record["invalid_markers"] == []
            and record["sourced_claim_rate_pct"] == VERIFICATION["sourced_claim_rate_pct"]
            and record["markers_found"] == VERIFICATION["markers_found"]
        )
        return Score(value="C" if ok else "I")

    return score


@solver
def echo_audit_record() -> Solver:
    async def solve(state, generate: Generate):
        # No model call. Echo the finished job's verification record.
        state.output = ModelOutput.from_content(model="oryx-audit-record", content=json.dumps(VERIFICATION))
        return state

    return solve


@task
def oryx_audit_check():
    return Task(
        dataset=MemoryDataset(
            [
                Sample(
                    input="What does recent evidence say about AI adoption in Indian healthcare, 2024-2026?",
                    target="PASS",
                    metadata=dict(VERIFICATION),
                )
            ]
        ),
        solver=echo_audit_record(),
        scorer=audit_scorer(),
    )
