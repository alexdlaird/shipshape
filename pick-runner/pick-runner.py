__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import datetime
import http.client
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from shipshape import fail, write_outputs


class HistoryUnreadable(Exception):
    pass


class Evidence(NamedTuple):
    job: Dict[str, Any]
    run: Dict[str, Any]

    @property
    def ran(self) -> bool:
        return bool(self.job.get("runner_name"))

    def describe(self) -> str:
        return f"{self.job['name']} in {self.run['name']} ({self.run['html_url']})"


def api_get(path: str) -> Any:
    request = urllib.request.Request(
        os.environ["GITHUB_API_URL"] + path,
        headers={
            "Authorization": f"Bearer {os.environ['TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as error:
        raise HistoryUnreadable(f"GET {path}: {error}") from error


def is_github_hosted(job: Dict[str, Any]) -> bool:
    return job["status"] == "completed" and bool(job["labels"]) and "self-hosted" not in job["labels"]


def was_refused_for_billing(repo_slug: str, job: Dict[str, Any], refused_message: str) -> bool:
    """
    A job GitHub refused for billing failed without ever getting a runner, and its check run says why.
    """
    if job["conclusion"] != "failure" or job.get("runner_name"):
        return False
    annotations = api_get(f"/repos/{repo_slug}/check-runs/{job['id']}/annotations")
    return any(refused_message in annotation["message"] for annotation in annotations)


def latest_evidence(repo_slug: str, since: str, runs_to_read: int, refused_message: str) -> Optional[Evidence]:
    """
    The repo's newest GitHub-hosted job since ``since`` that either ran or was refused for billing, reading each run's
    jobs across all its attempts, so a run's own earlier refusal counts.
    """
    runs = api_get(f"/repos/{repo_slug}/actions/runs?per_page={runs_to_read}&created=%3E%3D{since}")["workflow_runs"]
    for run in runs:
        jobs = api_get(f"/repos/{repo_slug}/actions/runs/{run['id']}/jobs?filter=all&per_page=100")["jobs"]
        hosted = sorted(filter(is_github_hosted, jobs), key=lambda job: job["completed_at"], reverse=True)
        for job in hosted:
            if job.get("runner_name") or was_refused_for_billing(repo_slug, job, refused_message):
                return Evidence(job, run)
    return None


def pick(force_self_hosted: bool, repo_slugs: List[str], since: str, runs_to_read: int, refused_message: str,
         on_error_self_hosted: bool) -> Tuple[bool, str]:
    if force_self_hosted:
        return True, "the override is on"
    try:
        found = [latest_evidence(repo_slug, since, runs_to_read, refused_message) for repo_slug in repo_slugs]
    except (HistoryUnreadable, KeyError, TypeError) as error:
        return on_error_self_hosted, f"the run history couldn't be read ({error})"

    evidence = [item for item in found if item]
    if not evidence:
        return False, f"no GitHub-hosted job since {since}"
    latest = max(evidence, key=lambda item: item.job["completed_at"])
    if latest.ran:
        return False, f"the latest GitHub-hosted job ran: {latest.describe()}"
    return True, f"the latest GitHub-hosted job was refused for billing: {latest.describe()}"


def main() -> None:
    on_error = os.environ.get("ON_ERROR", "self-hosted")
    if on_error not in ("self-hosted", "github-hosted"):
        fail(f"\"on-error\" must be \"self-hosted\" or \"github-hosted\", not {on_error}")
    repo_slugs = [line.strip() for line in os.environ["REPO_SLUGS"].splitlines() if line.strip()]
    since = os.environ.get("SINCE", "").strip() or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-01")

    self_hosted, reason = pick(os.environ.get("FORCE_SELF_HOSTED") == "true", repo_slugs, since,
                               int(os.environ.get("RUNS_TO_READ", "50")),
                               os.environ.get("REFUSED_MESSAGE") or "The job was not started because",
                               on_error == "self-hosted")

    runners = "Self-hosted runners" if self_hosted else "GitHub-hosted runners"
    print(f"::notice title=Runner::{runners}, because {reason}")
    write_outputs(self_hosted="true" if self_hosted else "false",
                  runs_on=os.environ["SELF_HOSTED_RUNS_ON"] if self_hosted else os.environ["GITHUB_HOSTED_RUNS_ON"],
                  reason=reason)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
        summary.write(f"### Runner: {runners}\n\nBecause {reason}.\n")


if __name__ == "__main__":
    main()
