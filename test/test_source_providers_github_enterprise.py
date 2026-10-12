"""GitHub Enterprise hosts: the ``dashboard.github_hosts`` allowlist end to end.

The allowlist mirrors ``dashboard.gitlab_hosts``: exact ``host[:port]`` match,
empty by default, config-only. These tests pin the guard rails -- an empty list
leaves every github.com path as it is, a lookalike host is never recognized, and
a host cannot be claimed by two provider allowlists.
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew.config import loader as config_loader
from kiro_crew.config import sections as config_sections
from kiro_crew.dashboard.source_providers import hosts, links
from kiro_crew.dashboard.source_providers.contract import RepoRef, SourceRef

GHE = "ghe.example.com"


@pytest.fixture
def ghe_listed(monkeypatch):
    """Seed the cached snapshot the sync parsers read, as a config load would."""
    monkeypatch.setattr(hosts, "_github_hosts_snapshot", frozenset({GHE}))


@pytest.fixture(autouse=True)
def _cold_allowlists(monkeypatch):
    monkeypatch.setattr(hosts, "_github_hosts_snapshot", frozenset())
    monkeypatch.setattr(hosts, "_gitlab_hosts_snapshot", frozenset())
    monkeypatch.setattr(hosts, "_jira_hosts_snapshot", frozenset())


# --- config ------------------------------------------------------------------


def test_github_hosts_defaults_to_empty() -> None:
    dashboard = config_loader._build_dashboard_config(set(), {})
    assert dashboard.github_hosts == []


def test_github_hosts_entries_are_normalized_like_gitlab_hosts() -> None:
    raw = [
        " GHE.Example.com ",
        "ghe.example.com",  # duplicate after normalization
        "ghe2.example.com:443",  # default port dropped
        "ghe3.example.com.:08443",  # trailing dot and leading-zero port
        "https://ghe4.example.com",  # scheme
        "*.example.com",  # wildcard
        "ghe5.example.com/path",  # path
        "user@ghe6.example.com",  # userinfo
        "ghe7.example.com:",  # empty port
        "github.com",  # needs no entry
        "www.github.com",
        "gitlab.com",  # another provider's public host
        42,
    ]
    assert config_sections._coerce_github_hosts(raw) == [
        "ghe.example.com",
        "ghe2.example.com",
        "ghe3.example.com:8443",
    ]
    assert config_sections._coerce_github_hosts("ghe.example.com") == []


def test_a_host_also_in_gitlab_hosts_is_refused_at_config_load(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        dashboard = config_loader._build_dashboard_config(
            set(),
            {
                "github_hosts": ["shared.example.com", GHE],
                "gitlab_hosts": ["shared.example.com"],
            },
        )
    assert dashboard.github_hosts == [GHE]
    # The GitLab side is untouched by the refusal.
    assert dashboard.gitlab_hosts == ["shared.example.com"]
    assert "shared.example.com" in caplog.text


def test_a_host_also_in_jira_hosts_is_refused_at_config_load() -> None:
    dashboard = config_loader._build_dashboard_config(
        set(),
        {"github_hosts": ["jira.example.com:8443"], "jira_hosts": ["JIRA.example.com:8443"]},
    )
    assert dashboard.github_hosts == []
    assert dashboard.jira_hosts == ["jira.example.com:8443"]


@pytest.mark.asyncio
async def test_the_refresh_publishes_github_hosts_and_bumps_the_generation(monkeypatch) -> None:
    def fake_load():
        return frozenset(), frozenset(), True, frozenset({GHE})

    monkeypatch.setattr(hosts, "_load_source_link_settings", fake_load)
    monkeypatch.setattr(hosts, "_gitlab_hosts_loaded_at", 0.0)
    monkeypatch.setattr(hosts, "_gitlab_hosts_generation", 3)

    assert hosts._allowed_github_hosts() == frozenset()
    await hosts.ensure_gitlab_hosts_loaded()
    assert hosts._allowed_github_hosts() == frozenset({GHE})
    # Cached parse decisions fold the generation in, so a newly listed host
    # must move it or a cold-snapshot rejection would stay memoized.
    assert hosts.gitlab_hosts_generation() == 4


def test_an_unreadable_config_leaves_github_hosts_closed(monkeypatch) -> None:
    def broken_load(*_a, **_k):
        raise OSError("torn read")

    monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(broken_load))
    assert hosts._load_source_link_settings()[3] == frozenset()


# --- parse_source_url ----------------------------------------------------------


def test_github_dot_com_parses_exactly_as_without_the_allowlist() -> None:
    expected = SourceRef(
        "github", "https://github.com/acme/repo/pull/12", "github.com", "acme", "repo", 12
    )
    assert links.parse_source_url("https://github.com/acme/repo/pull/12") == expected
    assert links.parse_source_url("https://www.github.com/acme/repo/pull/12/") == expected
    issue = links.parse_source_url("https://github.com/acme/repo/issues/3")
    assert (issue.host, issue.kind, issue.url) == (
        "github.com",
        "issue",
        "https://github.com/acme/repo/issues/3",
    )


def test_an_unlisted_enterprise_host_is_refused() -> None:
    with pytest.raises(ValueError):
        links.parse_source_url(f"https://{GHE}/acme/repo/pull/12")


@pytest.mark.usefixtures("ghe_listed")
def test_a_listed_enterprise_host_parses_as_github_and_keeps_its_host() -> None:
    ref = links.parse_source_url(f"https://{GHE.upper()}/acme/repo/pull/12")
    assert ref == SourceRef("github", f"https://{GHE}/acme/repo/pull/12", GHE, "acme", "repo", 12)
    issue = links.parse_source_url(f"https://{GHE}./acme/repo/issues/9")
    assert (issue.provider, issue.host, issue.kind, issue.number) == ("github", GHE, "issue", 9)
    assert issue.url == f"https://{GHE}/acme/repo/issues/9"
    # Explicit :443 is the same origin the browser URL API reports.
    assert links.parse_source_url(f"https://{GHE}:443/acme/repo/pull/12").url == (
        f"https://{GHE}/acme/repo/pull/12"
    )


@pytest.mark.usefixtures("ghe_listed")
def test_the_enterprise_identity_never_collides_with_github_dot_com() -> None:
    public = links.parse_source_url("https://github.com/acme/repo/pull/12")
    enterprise = links.parse_source_url(f"https://{GHE}/acme/repo/pull/12")
    assert public.identity != enterprise.identity


@pytest.mark.usefixtures("ghe_listed")
@pytest.mark.parametrize(
    "url",
    [
        "https://github.com.evil.example/acme/repo/pull/1",
        f"https://{GHE}.attacker/acme/repo/pull/1",
        f"https://evil-{GHE}/acme/repo/pull/1",
        f"https://sub.{GHE}/acme/repo/pull/1",
        "https://example.com/acme/repo/pull/1",
        f"https://evil.example/{GHE}/acme/repo/pull/1",
        f"https://evil.example/acme/repo/pull/1?u=https://{GHE}/acme/repo/pull/1",
        f"https://user@{GHE}/acme/repo/pull/1",
        f"http://{GHE}/acme/repo/pull/1",
        f"https://{GHE}:8443/acme/repo/pull/1",
    ],
)
def test_lookalike_and_unlisted_spellings_are_not_recognized(url: str) -> None:
    with pytest.raises(ValueError):
        links.parse_source_url(url)


def test_a_listed_port_is_required_and_matched_exactly(monkeypatch) -> None:
    monkeypatch.setattr(hosts, "_github_hosts_snapshot", frozenset({f"{GHE}:8443"}))
    ref = links.parse_source_url(f"https://{GHE}:8443/acme/repo/pull/1")
    assert (ref.host, ref.url) == (f"{GHE}:8443", f"https://{GHE}:8443/acme/repo/pull/1")
    with pytest.raises(ValueError):
        links.parse_source_url(f"https://{GHE}/acme/repo/pull/1")
    with pytest.raises(ValueError):
        links.parse_source_url(f"https://{GHE}:9443/acme/repo/pull/1")


@pytest.mark.usefixtures("ghe_listed")
def test_a_listed_enterprise_host_still_requires_the_github_path_grammar() -> None:
    for path in ("/acme/repo/-/merge_requests/1", "/acme/repo/pull/x", "/../repo/pull/1"):
        with pytest.raises(ValueError):
            links.parse_source_url(f"https://{GHE}{path}")


# --- parse_repo_url ------------------------------------------------------------


def test_repo_url_on_an_unlisted_enterprise_host_is_refused() -> None:
    with pytest.raises(ValueError):
        links.parse_repo_url(f"https://{GHE}/acme/repo")


@pytest.mark.usefixtures("ghe_listed")
def test_repo_url_on_a_listed_enterprise_host_keeps_its_host() -> None:
    assert links.parse_repo_url(f"https://{GHE}/acme/repo.git") == RepoRef(
        "github", GHE, "acme", "repo"
    )
    assert links.parse_repo_url("https://github.com/acme/repo") == RepoRef(
        "github", "github.com", "acme", "repo"
    )
    with pytest.raises(ValueError):
        links.parse_repo_url(f"https://{GHE}/acme/repo/pull/1")
    with pytest.raises(ValueError):
        links.parse_repo_url(f"https://{GHE}.attacker/acme/repo")
