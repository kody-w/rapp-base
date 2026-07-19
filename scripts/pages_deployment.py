#!/usr/bin/env python3
"""Decide whether a completed workflow should publish the current main branch."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

CI_WORKFLOW = "Read-only CI"
PROCESS_WORKFLOW = "Process RAPP Base requests"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class DeploymentDecision:
    deploy: bool
    reason: str
    message: str


def decide_pages_deployment(
    *,
    event_name: str,
    current_sha: str,
    workflow_name: str = "",
    workflow_event: str = "",
    workflow_conclusion: str = "",
    workflow_head_sha: str = "",
) -> DeploymentDecision:
    if _SHA_RE.fullmatch(current_sha) is None:
        raise ValueError("current main SHA is invalid")
    if event_name == "workflow_dispatch":
        return DeploymentDecision(
            True,
            "manual_dispatch",
            "Manual dispatch deploys current main.",
        )
    if event_name != "workflow_run":
        raise ValueError("unsupported Pages trigger")
    if workflow_conclusion != "success":
        raise ValueError("workflow run was not successful")
    if workflow_name == CI_WORKFLOW and workflow_event == "push":
        return DeploymentDecision(
            True,
            "successful_ci_push",
            "Successful main CI push deploys current main.",
        )
    if workflow_name != PROCESS_WORKFLOW:
        raise ValueError("unsupported completed workflow")
    if _SHA_RE.fullmatch(workflow_head_sha) is None:
        raise ValueError("processor head SHA is invalid")
    if current_sha == workflow_head_sha:
        return DeploymentDecision(
            False,
            "processor_no_commit",
            "Processor made no commit; Pages deployment is skipped.",
        )
    return DeploymentDecision(
        True,
        "processor_changed_main",
        "Processor changed main; Pages deploys current main.",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--current-sha", required=True)
    parser.add_argument("--workflow-name", default="")
    parser.add_argument("--workflow-event", default="")
    parser.add_argument("--workflow-conclusion", default="")
    parser.add_argument("--workflow-head-sha", default="")
    args = parser.parse_args()
    try:
        decision = decide_pages_deployment(
            event_name=args.event_name,
            current_sha=args.current_sha,
            workflow_name=args.workflow_name,
            workflow_event=args.workflow_event,
            workflow_conclusion=args.workflow_conclusion,
            workflow_head_sha=args.workflow_head_sha,
        )
    except ValueError as exc:
        print(f"Pages deployment decision failed: {exc}", file=sys.stderr)
        return 2
    print(f"deploy={str(decision.deploy).lower()}")
    print(f"reason={decision.reason}")
    print(f"message={decision.message}")
    print(f"current_sha={args.current_sha}")
    if args.workflow_head_sha:
        print(f"workflow_head_sha={args.workflow_head_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
