"""Review-workflow tests for what a review lane's runner can reach: the fork lanes'
network egress.
"""

from __future__ import annotations

import pytest
import yaml
from ai_review_workflows_helpers import (
    FORK_REVIEW_LANES,
    WORKFLOWS,
)


def _lane_jobs(lane: str) -> dict:
    """The `jobs:` mapping of one workflow file under `.github/workflows`."""
    return yaml.safe_load((WORKFLOWS / lane).read_text(encoding="utf-8"))["jobs"]


def _blocking_endpoints(job: dict) -> "list[str] | None":
    """The job's blocking-egress endpoint list, or None when it does not block."""
    for step in job.get("steps") or ():
        if "step-security/harden-runner" not in str(step.get("uses") or ""):
            continue
        settings = step.get("with") or {}
        if settings.get("egress-policy") != "block":
            return None
        return str(settings.get("allowed-endpoints") or "").split()
    return None


class TestForkLaneBunEgress:
    """The fork reviewers install bun from a GitHub *release asset*.

    `anthropics/claude-code-action` runs `oven-sh/setup-bun`, which downloads
    `https://github.com/oven-sh/bun/releases/download/...`. GitHub answers that
    with a 302 to `release-assets.githubusercontent.com` -- a different host
    from the `objects.githubusercontent.com` these allowlists already carry. A
    lane that blocks egress without it does not fail at the model call: bun
    never lands, the action's own script dies `bun: command not found`
    (exit 127), the lane posts `review incomplete`, and because `PR Readiness`
    aggregates these lanes, every fork PR goes red at once.

    `workflow_run` lanes always execute the DEFAULT branch's copy of the yaml,
    so a PR editing these files cannot exercise its own change. This test is
    the only pre-merge guard the coupling has.
    """

    ENDPOINT = "release-assets.githubusercontent.com:443"
    ACTION = "anthropics/claude-code-action"

    @classmethod
    def _runs_a_model(cls, job: dict) -> bool:
        return any(cls.ACTION in str(step.get("uses") or "") for step in job.get("steps") or ())

    @pytest.mark.parametrize("lane", FORK_REVIEW_LANES)
    def test_every_model_job_allows_the_bun_release_asset_host(self, lane: str) -> None:
        checked = 0
        for name, job in _lane_jobs(lane).items():
            if not self._runs_a_model(job):
                continue
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            checked += 1
            assert self.ENDPOINT in endpoints, (
                f"{lane} job {name!r} runs {self.ACTION} behind a blocking egress "
                f"policy but does not allow {self.ENDPOINT}, so setup-bun's download "
                "is refused and the step exits 127 instead of reviewing anything"
            )
        assert checked, f"{lane} has no blocking-egress {self.ACTION} job to check"

    @pytest.mark.parametrize("lane", FORK_REVIEW_LANES)
    def test_jobs_that_run_no_model_keep_the_narrower_allowlist(self, lane: str) -> None:
        # Least privilege: only the job that actually downloads bun gets the
        # host. `fork-security-scope-review.yml` blocks egress in four jobs and
        # runs the model in exactly one, so a blanket per-file edit would widen
        # three allowlists that fetch nothing but Actions artifacts.
        for name, job in _lane_jobs(lane).items():
            if self._runs_a_model(job):
                continue
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            assert self.ENDPOINT not in endpoints, (
                f"{lane} job {name!r} runs no model and downloads no bun, so "
                f"allowing {self.ENDPOINT} widens its egress for nothing"
            )


class TestForkLaneBubblewrapBootstrapEgress:
    """The fork reviewers fetch the whole toolchain their own settings turn on.

    Setting `allowed_non_write_users` auto-enables `claude-code-action`'s
    subprocess secret-scrub plus bubblewrap isolation, and the action bootstraps
    in two sequential network phases. First `apt-get install bubblewrap socat`:
    on the ubuntu-latest image `/etc/apt/apt-mirrors.txt` names the azure mirror
    first over plaintext http and falls back to the two canonical hosts over
    https, so all three are on the path of that one install. Then the CLI itself,
    via `curl https://claude.ai/install.sh`, whose script reads its version
    manifest and binary from `downloads.claude.ai/claude-code-releases`.

    Both phases are asserted together because they are SEQUENTIAL: an allowlist
    carrying only the apt half lets apt succeed and then dies on curl, with the
    same `review incomplete` and no model call, so a green apt phase is not
    evidence that the bootstrap resolves.

    Blocked, the install exits 7 before the model is ever reached, and the lane
    reports `review incomplete` rather than a verdict. Failing closed is
    correct -- the isolation is a security control, so running unsandboxed must
    never be a silent fallback -- but the lane then cannot review at all, and the
    advisory lanes publish that as a NEUTRAL check, so three reviewers stopped
    reviewing every fork PR without turning anything red.

    This is the whole-file guard: `workflow_run` lanes always execute the DEFAULT
    branch's yaml, so a PR editing these files cannot exercise its own change.
    The endpoints are asserted on the model job only, for the same least-privilege
    reason as the bun release-asset host above.
    """

    ENDPOINTS = (
        "azure.archive.ubuntu.com:80",
        "archive.ubuntu.com:443",
        "security.ubuntu.com:443",
        "claude.ai:443",
        "downloads.claude.ai:443",
    )
    ACTION = "anthropics/claude-code-action"

    @classmethod
    def _runs_a_model(cls, job: dict) -> bool:
        return any(cls.ACTION in str(step.get("uses") or "") for step in job.get("steps") or ())

    @pytest.mark.parametrize("lane", FORK_REVIEW_LANES)
    def test_every_model_job_allows_the_bubblewrap_bootstrap_hosts(self, lane: str) -> None:
        checked = 0
        for name, job in _lane_jobs(lane).items():
            if not self._runs_a_model(job):
                continue
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            checked += 1
            missing = [host for host in self.ENDPOINTS if host not in endpoints]
            assert not missing, (
                f"{lane} job {name!r} runs {self.ACTION} behind a blocking egress "
                f"policy but does not allow {missing}, so a phase of its bootstrap "
                "is refused (apt for the bubblewrap sandbox, curl for the CLI "
                "itself), the action exits 7 before any model call, and the lane "
                "publishes `review incomplete` instead of a verdict"
            )
        assert checked, f"{lane} has no blocking-egress {self.ACTION} job to check"

    @pytest.mark.parametrize("lane", FORK_REVIEW_LANES)
    def test_jobs_that_run_no_model_keep_the_narrower_allowlist(self, lane: str) -> None:
        # Least privilege, exactly as for the bun host: only a job that actually
        # bootstraps the sandbox gets the package mirrors.
        # `fork-security-scope-review.yml` blocks egress in four jobs and runs the
        # model in one, so a blanket per-file edit would widen three allowlists
        # that install nothing.
        for name, job in _lane_jobs(lane).items():
            if self._runs_a_model(job):
                continue
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            present = [host for host in self.ENDPOINTS if host in endpoints]
            assert not present, (
                f"{lane} job {name!r} runs no model and installs no sandbox, so "
                f"allowing {present} widens its egress for nothing"
            )

    @pytest.mark.parametrize("lane", FORK_REVIEW_LANES)
    def test_no_lane_allows_a_host_nothing_here_fetches(self, lane: str) -> None:
        # Two ways a reader widens this allowlist from something that merely
        # APPEARED in the output. The runner image preinstalls google-chrome and
        # microsoft apt sources, so a blocked `apt-get update` names them in the
        # same wall of text as the ubuntu archive -- but their failures are apt
        # WARNINGS (`W:`) and nothing here installs from them. And the CLI
        # installer script prints `code.claude.com` and `www.anthropic.com` inside
        # its own error messages without ever requesting them, so grepping that
        # script for hostnames yields two more that belong nowhere near a
        # blast-radius control.
        forbidden = (
            "dl.google.com",
            "packages.microsoft.com",
            "code.claude.com",
            "www.anthropic.com",
        )
        for name, job in _lane_jobs(lane).items():
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            for host in forbidden:
                assert not any(entry.startswith(host) for entry in endpoints), (
                    f"{lane} job {name!r} allows {host}, which this lane never "
                    "requests; it only ever appeared in a warning or an error string"
                )


class TestForkGptLaneMantleEgress:
    """The GPT passes reach Bedrock on the mantle host, not the runtime host.

    The two review passes run a CLI configured to talk to a loopback signing
    proxy; that proxy SigV4-signs each request and forwards it to
    `https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses` -- the URL a
    real job log shows the lane calling. The classic
    `bedrock-runtime.*.amazonaws.com` hosts in the allowlist do not cover it, so
    under blocking egress the proxy cannot connect, each pass retries five times,
    ends `Connection failed: error sending request`, and the lane fails closed
    with `review incomplete`.

    Only the job that runs that proxy gets the host: the Opus, Design, UX,
    First-Principles and Security-Scope lanes talk to Bedrock through the runtime
    host and must not carry it.
    """

    ENDPOINT = "bedrock-mantle.us-east-1.api.aws:443"
    # The lane that reaches the mantle host does so either by configuring the
    # built-in amazon-bedrock provider or by running the loopback signing proxy
    # that forwards to mantle (the UID-boundary design).
    PROVIDER = 'model_provider = "amazon-bedrock"'
    PROXY_MARKER = "bedrock-signing-proxy.py"

    @classmethod
    def _configures_the_mantle_provider(cls, job: dict) -> bool:
        for step in job.get("steps") or ():
            run = str(step.get("run") or "")
            if cls.PROVIDER in run or cls.PROXY_MARKER in run:
                return True
        return False

    def test_the_gpt_lane_model_job_allows_the_mantle_endpoint(self) -> None:
        checked = 0
        for name, job in _lane_jobs("fork-gpt-review.yml").items():
            if not self._configures_the_mantle_provider(job):
                continue
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            checked += 1
            assert self.ENDPOINT in endpoints, (
                f"fork-gpt-review.yml job {name!r} reaches "
                f"Bedrock's mantle endpoint behind a blocking egress policy but "
                f"does not allow {self.ENDPOINT}, so the signing proxy fails to "
                "connect and the lane posts `review incomplete`"
            )
        assert checked, "fork-gpt-review.yml has no blocking-egress mantle job to check"

    @pytest.mark.parametrize("lane", FORK_REVIEW_LANES)
    def test_lanes_that_use_no_mantle_provider_keep_the_narrower_allowlist(self, lane: str) -> None:
        for name, job in _lane_jobs(lane).items():
            if self._configures_the_mantle_provider(job):
                continue
            endpoints = _blocking_endpoints(job)
            if endpoints is None:
                continue
            assert self.ENDPOINT not in endpoints, (
                f"{lane} job {name!r} runs no mantle-backed model, so allowing "
                f"{self.ENDPOINT} widens its egress for nothing"
            )
