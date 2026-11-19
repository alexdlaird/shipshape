__copyright__ = "Copyright (c) 2026 Alex Laird"
__license__ = "MIT"

import datetime
import json
import os
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.testcase import ActionTestCase

REPO = "owner/app"
OTHER_REPO = "owner/other"
SINCE = "2026-11-01"
REFUSED = ("The job was not started because recent account payments have failed or your spending limit needs to be "
           "increased.")


def month_start():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-01")


def runs_path(repo, since=SINCE, runs_to_read=50):
    return f"/repos/{repo}/actions/runs?per_page={runs_to_read}&created=%3E%3D{since}"


def jobs_path(repo, run_id):
    return f"/repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100"


def annotations_path(repo, job_id):
    return f"/repos/{repo}/check-runs/{job_id}/annotations"


def ran_job(job_id, completed_at, labels=("ubuntu-latest",)):
    return {"id": job_id, "name": f"Job {job_id}", "status": "completed", "conclusion": "success",
            "labels": list(labels), "runner_name": f"runner-{job_id}", "completed_at": completed_at}


def unstarted_job(job_id, completed_at):
    return {"id": job_id, "name": f"Job {job_id}", "status": "completed", "conclusion": "failure",
            "labels": ["ubuntu-latest"], "runner_name": None, "completed_at": completed_at}


class FakeGitHub:
    def __init__(self):
        self.routes = {}
        self.requests = []
        self.status = 200
        self.truncated = False
        routes, requests = self.routes, self.requests
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                requests.append((self.path, self.headers.get("Authorization")))
                if fake.status != 200 or self.path not in routes:
                    self.send_response(fake.status if fake.status != 200 else 404)
                    self.end_headers()
                    return
                body = json.dumps(routes[self.path]).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body) * 2 if fake.truncated else len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def paths(self):
        return [path for path, _ in self.requests]


class TestPickRunner(ActionTestCase):
    def setUp(self):
        super().setUp()
        self.github = FakeGitHub()
        self.summary = os.path.join(tempfile.mkdtemp(), "summary")
        open(self.summary, "w").close()

    def tearDown(self):
        self.github.stop()
        shutil.rmtree(os.path.dirname(self.summary), ignore_errors=True)
        super().tearDown()

    def given_runs(self, *runs, repo=REPO, since=SINCE, runs_to_read=50):
        self.github.routes[runs_path(repo, since, runs_to_read)] = {
            "workflow_runs": [{"id": run_id, "name": f"Run {run_id}",
                               "html_url": f"https://github.com/{repo}/actions/runs/{run_id}"}
                              for run_id, _ in runs]}
        for run_id, jobs in runs:
            self.github.routes[jobs_path(repo, run_id)] = {"jobs": jobs}
            for job in jobs:
                self.given_annotations(job["id"], [], repo=repo)

    def given_annotations(self, job_id, messages, repo=REPO):
        self.github.routes[annotations_path(repo, job_id)] = [{"message": message} for message in messages]

    def given_refused(self, job_id, repo=REPO):
        self.given_annotations(job_id, [REFUSED], repo=repo)

    def pick_runner(self, **inputs):
        env = {"TOKEN": "the-token", "GITHUB_API_URL": self.github.url, "GITHUB_STEP_SUMMARY": self.summary,
               "REPO_SLUGS": REPO, "SINCE": SINCE, "GITHUB_HOSTED_RUNS_ON": '"ubuntu-latest"',
               "SELF_HOSTED_RUNS_ON": '["self-hosted"]'}
        env.update({name.upper(): value for name, value in inputs.items()})
        return self.run_action("pick-runner", **env)

    def assert_picks(self, result, self_hosted):
        self.assert_succeeds(result)
        self.assertEqual("true" if self_hosted else "false", self.outputs()["self_hosted"], self.outputs()["reason"])

    def test_override_picks_self_hosted_without_reading_history(self):
        # WHEN
        result = self.pick_runner(force_self_hosted="true")

        # THEN
        self.assert_picks(result, self_hosted=True)
        self.assertEqual("the override is on", self.outputs()["reason"])
        self.assertEqual([], self.github.requests)

    def test_no_runs_this_cycle_picks_github_hosted(self):
        # GIVEN
        self.given_runs()

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_picks(result, self_hosted=False)
        self.assertEqual(f"no GitHub-hosted job since {SINCE}", self.outputs()["reason"])

    def test_latest_hosted_job_ran_picks_github_hosted(self):
        # GIVEN
        self.given_runs((10, [ran_job(1, "2026-11-10T00:00:00Z")]))

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_picks(result, self_hosted=False)
        self.assertEqual(
            f"the latest GitHub-hosted job ran: Job 1 in Run 10 (https://github.com/{REPO}/actions/runs/10)",
            self.outputs()["reason"])

    def test_latest_hosted_job_refused_picks_self_hosted(self):
        # GIVEN
        self.given_runs((10, [unstarted_job(1, "2026-11-10T00:00:00Z")]))
        self.given_refused(1)

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_picks(result, self_hosted=True)
        self.assertIn("was refused for billing: Job 1 in Run 10", self.outputs()["reason"])

    def test_refusal_newer_than_a_job_that_ran_picks_self_hosted(self):
        # GIVEN
        self.given_runs((10, [ran_job(1, "2026-11-10T00:00:00Z"), unstarted_job(2, "2026-11-10T00:05:00Z")]))
        self.given_refused(2)

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_picks(result, self_hosted=True)

    def test_job_that_ran_newer_than_a_refusal_picks_github_hosted(self):
        # GIVEN
        self.given_runs((10, [unstarted_job(1, "2026-11-10T00:00:00Z"), ran_job(2, "2026-11-10T00:05:00Z")]))
        self.given_refused(1)

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_picks(result, self_hosted=False)

    def test_unstarted_failure_without_the_billing_message_is_skipped(self):
        # GIVEN
        self.given_runs((10, [unstarted_job(1, "2026-11-10T00:00:00Z"), unstarted_job(2, "2026-11-10T00:05:00Z")]))
        self.given_refused(1)
        self.given_annotations(2, ["The operation was canceled."])

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_picks(result, self_hosted=True)
        self.assertIn("Job 1", self.outputs()["reason"])

    def test_cancelled_job_without_a_runner_is_skipped_without_reading_annotations(self):
        # GIVEN
        cancelled = dict(unstarted_job(2, "2026-11-10T00:05:00Z"), conclusion="cancelled")
        self.given_runs((10, [ran_job(1, "2026-11-10T00:00:00Z"), cancelled]))

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_picks(result, self_hosted=False)
        self.assertNotIn(annotations_path(REPO, 2), self.github.paths())

    def test_self_hosted_and_unlabeled_jobs_are_ignored(self):
        # GIVEN
        self.given_runs((10, [unstarted_job(1, "2026-11-10T00:00:00Z"),
                              ran_job(2, "2026-11-10T00:05:00Z", labels=("self-hosted", "macos")),
                              dict(ran_job(4, "2026-11-10T00:06:00Z"), labels=[])]))
        self.given_refused(1)

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_picks(result, self_hosted=True)

    def test_unfinished_job_is_ignored(self):
        # GIVEN
        queued = dict(unstarted_job(2, None), status="queued", conclusion=None)
        self.given_runs((10, [ran_job(1, "2026-11-10T00:00:00Z"), queued]))

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_picks(result, self_hosted=False)

    def test_newest_run_with_evidence_decides(self):
        # GIVEN
        cancelled = dict(unstarted_job(1, "2026-11-12T00:00:00Z"), conclusion="cancelled")
        self.given_runs((11, [cancelled]), (10, [unstarted_job(2, "2026-11-10T00:00:00Z")]),
                        (9, [ran_job(3, "2026-11-09T00:00:00Z")]))
        self.given_refused(2)

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_picks(result, self_hosted=True)
        self.assertNotIn(jobs_path(REPO, 9), self.github.paths())

    def test_reads_every_attempt_and_sends_the_token(self):
        # GIVEN
        self.given_runs((10, [ran_job(1, "2026-11-10T00:00:00Z")]))

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_succeeds(result)
        self.assertIn(jobs_path(REPO, 10), self.github.paths())
        self.assertEqual({"Bearer the-token"}, {authorization for _, authorization in self.github.requests})

    def test_blank_since_reads_from_the_first_of_this_month(self):
        # GIVEN
        self.given_runs((10, [ran_job(1, "2026-11-10T00:00:00Z")]), since=month_start())

        # WHEN
        result = self.pick_runner(since="")

        # THEN
        self.assert_picks(result, self_hosted=False)
        self.assertEqual(runs_path(REPO, month_start()), self.github.paths()[0])

    def test_since_and_runs_to_read_are_used_as_given(self):
        # GIVEN
        self.given_runs((10, [ran_job(1, "2026-10-20T00:00:00Z")]), since="2026-10-15", runs_to_read=20)

        # WHEN
        result = self.pick_runner(since="2026-10-15", runs_to_read="20")

        # THEN
        self.assert_picks(result, self_hosted=False)

    def test_custom_refused_message(self):
        # GIVEN
        self.given_runs((10, [unstarted_job(1, "2026-11-10T00:00:00Z")]))
        self.given_annotations(1, ["Billing says no."])

        # WHEN
        result = self.pick_runner(refused_message="Billing says no")

        # THEN
        self.assert_picks(result, self_hosted=True)

    def test_refusal_in_another_repo_newer_than_this_repo_ran_picks_self_hosted(self):
        # GIVEN
        self.given_runs((10, [ran_job(1, "2026-11-10T00:00:00Z")]))
        self.given_runs((20, [unstarted_job(2, "2026-11-11T00:00:00Z")]), repo=OTHER_REPO)
        self.given_refused(2, repo=OTHER_REPO)

        # WHEN
        result = self.pick_runner(repo_slugs=f"{REPO}\n\n{OTHER_REPO}\n")

        # THEN
        self.assert_picks(result, self_hosted=True)
        self.assertIn(f"https://github.com/{OTHER_REPO}/actions/runs/20", self.outputs()["reason"])

    def test_ran_in_another_repo_newer_than_this_repo_refused_picks_github_hosted(self):
        # GIVEN
        self.given_runs((10, [unstarted_job(1, "2026-11-10T00:00:00Z")]))
        self.given_refused(1)
        self.given_runs((20, [ran_job(2, "2026-11-11T00:00:00Z")]), repo=OTHER_REPO)

        # WHEN
        result = self.pick_runner(repo_slugs=f"{REPO}\n{OTHER_REPO}")

        # THEN
        self.assert_picks(result, self_hosted=False)

    def test_unreadable_history_picks_self_hosted_by_default(self):
        # GIVEN
        self.github.status = 500

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_picks(result, self_hosted=True)
        self.assertIn("the run history couldn't be read (GET /repos/owner/app/actions/runs", self.outputs()["reason"])
        self.assertIn("500", self.outputs()["reason"])

    def test_unreadable_history_picks_github_hosted_when_asked(self):
        # GIVEN
        self.github.status = 500

        # WHEN
        result = self.pick_runner(on_error="github-hosted")

        # THEN
        self.assert_picks(result, self_hosted=False)

    def test_truncated_response_counts_as_unreadable(self):
        # GIVEN
        self.given_runs()
        self.github.truncated = True

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_picks(result, self_hosted=True)
        self.assertIn("couldn't be read", self.outputs()["reason"])

    def test_unexpected_response_counts_as_unreadable(self):
        # GIVEN
        self.github.routes[runs_path(REPO)] = {"message": "Bad credentials"}

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_picks(result, self_hosted=True)
        self.assertIn("couldn't be read", self.outputs()["reason"])

    def test_runs_on_is_the_picked_runners_labels(self):
        # GIVEN
        self.given_runs((10, [unstarted_job(1, "2026-11-10T00:00:00Z")]))
        self.given_refused(1)

        # WHEN
        result = self.pick_runner(self_hosted_runs_on='["self-hosted", "macos"]',
                                  github_hosted_runs_on='"ubuntu-24.04"')

        # THEN
        self.assert_picks(result, self_hosted=True)
        self.assertEqual(["self-hosted", "macos"], json.loads(self.outputs()["runs_on"]))

    def test_runs_on_defaults(self):
        # GIVEN
        self.given_runs()

        # WHEN
        github_hosted = self.pick_runner()
        github_hosted_runs_on = self.outputs()["runs_on"]
        self_hosted = self.pick_runner(force_self_hosted="true")

        # THEN
        self.assert_succeeds(github_hosted)
        self.assert_succeeds(self_hosted)
        self.assertEqual("ubuntu-latest", json.loads(github_hosted_runs_on))
        self.assertEqual(["self-hosted"], json.loads(self.outputs()["runs_on"]))

    def test_writes_a_notice_and_the_step_summary(self):
        # GIVEN
        self.given_runs()

        # WHEN
        result = self.pick_runner()

        # THEN
        self.assert_succeeds(result)
        self.assertIn(f"::notice title=Runner::GitHub-hosted runners, because no GitHub-hosted job since {SINCE}",
                      result.stdout)
        with open(self.summary) as f:
            self.assertEqual(f"### Runner: GitHub-hosted runners\n\nBecause no GitHub-hosted job since {SINCE}.\n",
                             f.read())

    def test_invalid_on_error_fails(self):
        # WHEN
        result = self.pick_runner(on_error="mac")

        # THEN
        self.assert_fails(result)
        self.assertIn('::error::"on-error" must be "self-hosted" or "github-hosted", not mac', result.stdout)
