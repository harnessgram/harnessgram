from pathlib import Path


def test_self_hosted_pull_request_jobs_reject_forks():
    workflow = Path(".github/workflows/tests.yml").read_text(encoding="utf-8")
    guard = "github.event.pull_request.head.repo.full_name == github.repository"
    self_hosted_jobs = workflow.count("runs-on: [self-hosted")
    assert self_hosted_jobs
    assert workflow.count(guard) == self_hosted_jobs
