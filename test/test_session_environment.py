"""Environment contract tests without provider processes or cloud access."""

from __future__ import annotations

import math
from dataclasses import FrozenInstanceError, dataclass
from unittest.mock import Mock

import pytest

from kiro_crew.session_environment import (
    SessionEnvironmentBinding,
    SessionEnvironmentConfiguration,
    SessionEnvironmentHealth,
    SessionEnvironmentHealthProvider,
    SessionEnvironmentLifecycleProvider,
    SessionEnvironmentMemoryHealth,
    SessionEnvironmentPrefetchProvider,
    SessionEnvironmentPrefetchUnavailable,
    SessionEnvironmentRegistry,
    SessionEnvironmentSelection,
    SessionEnvironmentUnavailable,
)


@dataclass
class CatalogProvider:
    provider_id: str = "fixture"
    display_name: str = "Fixture Compute"
    icon: str = "server"
    secret: str = "fixture-secret-never-projected"

    def configurations(self) -> tuple[SessionEnvironmentConfiguration, ...]:
        return (
            SessionEnvironmentConfiguration("", "Default"),
            SessionEnvironmentConfiguration("large", "Large"),
        )

    def validate_configuration(self, configuration: str) -> str:
        if configuration not in {"", "large"}:
            raise ValueError("unknown configuration")
        return configuration

    def binding_for_session(self, session_key: str) -> SessionEnvironmentBinding | None:
        return None

    def create_session_host(self, session_key: str, configuration: str) -> object:
        raise AssertionError("catalog and prefetch must not allocate compute")

    async def request_stop_for_session(self, session_key: str) -> str | None:
        raise AssertionError("catalog must not request a stop")

    async def stop_for_session(self, session_key: str) -> str | None:
        raise AssertionError("catalog must not stop compute")


class ReconnectProvider(CatalogProvider, SessionEnvironmentPrefetchProvider):
    def can_prefetch_session(self, session_key: str) -> bool:
        return session_key == "dashboard:ready"

    def create_prefetch_session_host(self, session_key: str, configuration: str) -> object:
        if not self.can_prefetch_session(session_key):
            raise SessionEnvironmentPrefetchUnavailable("resource stopped")
        return (session_key, configuration)


class TestBinding:
    def test_round_trip_preserves_selection(self):
        binding = SessionEnvironmentBinding("coder", "large", "crew-session-fixture")
        assert SessionEnvironmentBinding.from_dict(binding.to_dict()) == binding
        assert binding.selection == SessionEnvironmentSelection("coder", "large")
        with pytest.raises(FrozenInstanceError):
            binding.provider = "other"

    def test_only_public_identity_is_projected(self):
        binding = SessionEnvironmentBinding.from_dict(
            {
                "provider": "coder",
                "configuration": "large",
                "resource_name": "crew-session-fixture",
                "token": "fixture-secret",
                "owner": "private-owner",
                "uuid": "private-uuid",
                "template": "private-template",
            }
        )
        assert binding is not None
        assert binding.to_dict() == {
            "provider": "coder",
            "configuration": "large",
            "resource_name": "crew-session-fixture",
        }

    @pytest.mark.parametrize("value", [None, [], "coder", {}, {"provider": "coder"}])
    def test_incomplete_wire_value_is_not_a_binding(self, value):
        assert SessionEnvironmentBinding.from_dict(value) is None

    @pytest.mark.parametrize("field", ["provider", "configuration", "resource_name"])
    @pytest.mark.parametrize("value", [None, [], {}, 1, False])
    def test_wire_fields_must_be_strings(self, field, value):
        record = {"provider": "coder", "configuration": "", "resource_name": ""}
        record[field] = value
        assert SessionEnvironmentBinding.from_dict(record) is None

    @pytest.mark.parametrize("value", ["", "Coder", "x/y", "x y", "x\n", "a" * 65, None])
    def test_provider_validation(self, value):
        with pytest.raises(ValueError, match="provider"):
            SessionEnvironmentSelection(value)
        with pytest.raises(ValueError, match="provider"):
            SessionEnvironmentBinding(value)

    @pytest.mark.parametrize("value", ["x/y", "x y", "x\n", "a" * 65, None, 0])
    def test_configuration_validation(self, value):
        with pytest.raises(ValueError, match="configuration"):
            SessionEnvironmentSelection("coder", value)
        with pytest.raises(ValueError, match="configuration"):
            SessionEnvironmentBinding("coder", value)
        with pytest.raises(ValueError, match="configuration"):
            SessionEnvironmentConfiguration(value, "Large")

    @pytest.mark.parametrize("value", ["x\n", "x\r", "x\x00", "a" * 256, None])
    def test_resource_name_validation(self, value):
        with pytest.raises(ValueError, match="resource_name"):
            SessionEnvironmentBinding("coder", resource_name=value)

    def test_empty_resource_name_represents_allocation_intent(self):
        binding = SessionEnvironmentBinding("coder")
        assert binding.resource_name == ""
        assert binding.selection == SessionEnvironmentSelection("coder")

    @pytest.mark.parametrize("name", ["", None, "a" * 256])
    def test_configuration_requires_bounded_name(self, name):
        with pytest.raises(ValueError, match="configuration name"):
            SessionEnvironmentConfiguration("", name)


class TestRegistry:
    def test_catalog_is_public_and_does_not_start_or_stop_resources(self):
        provider = CatalogProvider()
        registry = SessionEnvironmentRegistry([provider])
        expected = [
            {
                "id": "fixture",
                "name": "Fixture Compute",
                "icon": "server",
                "configurations": [{"id": "", "name": "Default"}, {"id": "large", "name": "Large"}],
            }
        ]
        assert registry.catalog() == expected
        assert registry.get("fixture") is provider
        assert registry.providers() == (provider,)
        # Callers can edit their response without mutating the registry's catalog.
        registry.catalog()[0]["name"] = "changed"
        assert registry.catalog() == expected

    def test_duplicate_provider_is_refused(self):
        with pytest.raises(ValueError, match="duplicate"):
            SessionEnvironmentRegistry([CatalogProvider(), CatalogProvider()])

    def test_invalid_provider_is_refused(self):
        with pytest.raises(ValueError, match="provider"):
            SessionEnvironmentRegistry([CatalogProvider(provider_id="invalid/provider")])

    def test_missing_default_is_refused(self):
        with pytest.raises(ValueError, match="default environment provider is unavailable"):
            SessionEnvironmentRegistry([], default_provider_id="coder")

    def test_unknown_provider_does_not_fall_back_to_default(self):
        registry = SessionEnvironmentRegistry([ReconnectProvider()], default_provider_id="fixture")
        assert registry.get("missing") is None
        with pytest.raises(SessionEnvironmentUnavailable, match="missing"):
            registry.require("missing")
        selection = SessionEnvironmentSelection("missing")
        assert registry.supports_prefetch("dashboard:ready", selection) is False
        with pytest.raises(SessionEnvironmentUnavailable, match="missing"):
            registry.prefetch_host("dashboard:ready", selection)

    def test_empty_registry_does_not_allocate(self):
        registry = SessionEnvironmentRegistry([])
        assert registry.catalog() == []
        assert registry.supports_prefetch("dashboard:ready", None) is False
        with pytest.raises(SessionEnvironmentPrefetchUnavailable):
            registry.prefetch_host("dashboard:ready", None)

    def test_method_names_do_not_grant_prefetch_authority(self):
        provider = CatalogProvider()
        provider.can_prefetch_session = Mock(return_value=True)
        provider.create_prefetch_session_host = Mock(return_value=object())
        registry = SessionEnvironmentRegistry([provider], default_provider_id="fixture")
        assert registry.supports_prefetch("dashboard:ready", None) is False
        with pytest.raises(SessionEnvironmentPrefetchUnavailable):
            registry.prefetch_host("dashboard:ready", None)
        provider.can_prefetch_session.assert_not_called()
        provider.create_prefetch_session_host.assert_not_called()
        assert not isinstance(provider, SessionEnvironmentLifecycleProvider)
        assert not isinstance(provider, SessionEnvironmentHealthProvider)

    def test_explicit_provider_takes_precedence(self):
        selected = ReconnectProvider(provider_id="selected")
        registry = SessionEnvironmentRegistry(
            [CatalogProvider(), selected], default_provider_id="fixture"
        )
        selection = SessionEnvironmentSelection("selected", "large")
        assert registry.supports_prefetch("dashboard:ready", selection) is True
        assert registry.prefetch_host("dashboard:ready", selection) == ("dashboard:ready", "large")
        assert registry.supports_prefetch("dashboard:ready", None) is False

    def test_prefetch_uses_default_only_without_explicit_selection(self):
        registry = SessionEnvironmentRegistry([ReconnectProvider()], default_provider_id="fixture")
        assert registry.supports_prefetch("dashboard:ready", None) is True
        assert registry.prefetch_host("dashboard:ready", None) == ("dashboard:ready", "")
        assert registry.supports_prefetch("dashboard:cold", None) is False
        with pytest.raises(SessionEnvironmentPrefetchUnavailable, match="resource stopped"):
            registry.prefetch_host("dashboard:cold", None)

    def test_prefetch_revalidates_configuration(self):
        registry = SessionEnvironmentRegistry([ReconnectProvider()])
        with pytest.raises(ValueError, match="unknown configuration"):
            registry.prefetch_host(
                "dashboard:ready", SessionEnvironmentSelection("fixture", "gone")
            )

    def test_resource_stopping_after_prefetch_check_does_not_allocate(self):
        provider = ReconnectProvider()
        registry = SessionEnvironmentRegistry([provider], default_provider_id="fixture")
        assert registry.supports_prefetch("dashboard:ready", None) is True
        provider.can_prefetch_session = Mock(return_value=False)
        with pytest.raises(SessionEnvironmentPrefetchUnavailable, match="resource stopped"):
            registry.prefetch_host("dashboard:ready", None)


class TestHealth:
    @pytest.mark.parametrize("state", ["starting", "running", "stopped", "unavailable"])
    def test_state_is_reportable_without_memory(self, state):
        health = SessionEnvironmentHealth("coder", "crew-session-fixture", state)
        assert health.to_dict() == {
            "provider": "coder",
            "resource_name": "crew-session-fixture",
            "state": state,
        }

    def test_memory_is_an_optional_independent_sample(self):
        memory = SessionEnvironmentMemoryHealth(2.0, 8.0, 75.0, "normal")
        health = SessionEnvironmentHealth("coder", "crew-session-fixture", "running", memory)
        assert health.to_dict()["memory"] == {
            "available_gb": 2.0,
            "total_gb": 8.0,
            "used_percent": 75.0,
            "pressure": "normal",
        }

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, -1, None, True, "8"])
    @pytest.mark.parametrize("field", ["available_gb", "total_gb", "used_percent"])
    def test_invalid_numeric_sample_is_refused(self, field, value):
        sample = {"available_gb": 2.0, "total_gb": 8.0, "used_percent": 75.0, "pressure": "normal"}
        sample[field] = value
        with pytest.raises(ValueError, match="memory health"):
            SessionEnvironmentMemoryHealth(**sample)

    @pytest.mark.parametrize(
        "values",
        [(9, 8, 0, "normal"), (0, 0, 0, "normal"), (0, 8, 101, "critical"), (0, 8, 100, "unknown")],
    )
    def test_inconsistent_memory_sample_is_refused(self, values):
        with pytest.raises(ValueError, match="memory health"):
            SessionEnvironmentMemoryHealth(*values)

    @pytest.mark.parametrize("state", ["unknown", "deleted", "", None, []])
    def test_unknown_state_is_refused(self, state):
        with pytest.raises(ValueError, match="state"):
            SessionEnvironmentHealth("coder", "", state)

    @pytest.mark.asyncio
    async def test_capability_defaults_do_not_perform_work(self):
        health = SessionEnvironmentHealthProvider()
        with pytest.raises(SessionEnvironmentUnavailable):
            await health.health_for_session("dashboard:fixture")
        lifecycle = SessionEnvironmentLifecycleProvider()
        assert lifecycle.lifecycle_interval_seconds is None
        assert lifecycle.runtime_warm_seconds is None
        assert await lifecycle.reconcile_lifecycle() is None
        assert await lifecycle.shutdown_lifecycle() is None
        prefetch = SessionEnvironmentPrefetchProvider()
        assert prefetch.can_prefetch_session("dashboard:fixture") is False
        with pytest.raises(SessionEnvironmentPrefetchUnavailable):
            prefetch.create_prefetch_session_host("dashboard:fixture", "")
