"""Claim and execute persistent restricted factor experiments."""
from __future__ import annotations

import argparse
import json
import socket

from server.models.database import SessionLocal, init_db
from server.services.factor_research import claim_next_experiment, execute_experiment


def run_once(worker_id: str) -> dict:
    init_db()
    with SessionLocal() as db:
        row = claim_next_experiment(db, worker_id=worker_id)
        if row is None:
            return {"status": "idle", "worker_id": worker_id}
        experiment_id = row.id
        try:
            report = execute_experiment(
                db, experiment_id, worker_id=worker_id
            )
        except Exception as exc:
            return {
                "status": "failed",
                "worker_id": worker_id,
                "experiment_id": experiment_id,
                "error": f"{type(exc).__name__}: {exc}",
            }
        return {
            "status": "completed",
            "worker_id": worker_id,
            "experiment_id": experiment_id,
            "report": report,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--worker-id", default=f"{socket.gethostname()}-factor-worker"
    )
    parser.add_argument(
        "--max-jobs", type=int, default=1,
        help="Maximum queued experiments to execute before exiting (1-100).",
    )
    args = parser.parse_args()
    if not 1 <= args.max_jobs <= 100:
        parser.error("--max-jobs must be in [1, 100]")
    for _ in range(args.max_jobs):
        result = run_once(args.worker_id)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        if result["status"] == "idle":
            break


if __name__ == "__main__":
    main()
