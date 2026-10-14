"""Tests for ACP client: compaction failure verdicts and their transient classification."""

from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture


class TestCompactionVerdictReachesTheConsumer:
    """The verdict is SET on the client/handle and READ off the provider the
    dashboard was handed, so every hop between them has to forward it. Without
    the forwarding the retry is dead code: the read falls to its default and a
    throttled compaction is indistinguishable from an overflowing one."""

    def test_the_abc_declares_the_capability_with_a_safe_default(self):
        """Declared on LLMProvider, not only on the Acp providers: the dashboard
        reads this off whatever provider it holds, so an adapter that never sets
        it must answer "permanent" from the contract rather than from a getattr
        default nobody wrote down. False is the safe value — it gives up the turn
        exactly as it did before the capability existed."""
        from kiro_crew.providers.base import LLMProvider

        assert isinstance(LLMProvider.last_compaction_transient, property)
        # Read through the descriptor rather than an instance: LLMProvider is
        # abstract, and what matters is the value the ABC itself answers with for
        # an adapter that does not override.
        assert LLMProvider.last_compaction_transient.fget(object()) is False
        # The VERDICT is the whole contract: the reason text is deliberately not
        # forwarded, because the chat row gets it from the compaction-status
        # event title and the log from each arming site's own WARNING.
        assert not hasattr(LLMProvider, "last_compaction_failure")

    def test_the_provider_forwards_the_verdict_from_its_inner_client(self):
        from types import SimpleNamespace

        from kiro_crew.providers.acp import AcpProvider

        prov = AcpProvider.__new__(AcpProvider)
        prov._client = SimpleNamespace(last_compaction_transient=True)
        assert prov.last_compaction_transient is True

    def test_the_session_provider_forwards_the_verdict_from_its_handle(self):
        from types import SimpleNamespace

        from kiro_crew.acp.session_provider import AcpSessionProvider

        sess = AcpSessionProvider.__new__(AcpSessionProvider)
        sess._handle = SimpleNamespace(last_compaction_transient=True)
        assert sess.last_compaction_transient is True

    def test_a_client_without_the_fields_reads_as_permanent(self):
        """A backend or a placeholder client that never set them must read as
        "not retryable" rather than raising — the fields are additive."""
        from types import SimpleNamespace

        from kiro_crew.providers.acp import AcpProvider

        prov = AcpProvider.__new__(AcpProvider)
        prov._client = SimpleNamespace()
        assert prov.last_compaction_transient is False

    def test_a_truthy_stand_in_is_not_a_verdict(self):
        """Coerced to a real bool at the boundary: an auto-created attribute (a
        Mock child, say) is truthy, and reading one as "transient" would replay
        a turn whose compaction genuinely could not succeed."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from kiro_crew.providers.acp import AcpProvider

        prov = AcpProvider.__new__(AcpProvider)
        prov._client = SimpleNamespace(last_compaction_transient=MagicMock())
        assert prov.last_compaction_transient is False


class TestCompactionFailureIsTransient:
    """The verdict that splits "this will fail again identically" from "this had
    nothing wrong with it". Read from the payload, never the rendered notice —
    that text is truncated, redacted, and sometimes only a raw repr."""

    def test_a_throttled_summarization_call_is_transient(self):
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert compaction_failure_is_transient(
            {
                "name": "ModelThrottleError",
                "cause": {"reason": "MODEL_TEMPORARILY_UNAVAILABLE"},
            }
        )

    def test_the_enum_spelling_of_the_reason_is_transient(self):
        """The machine reason arrives SCREAMING_SNAKE while the sentence beside
        it is prose, so a marker list matching only one spelling would classify
        the same fault differently depending on which field was filled."""
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert compaction_failure_is_transient({"reason": "MODEL_TEMPORARILY_UNAVAILABLE"})

    def test_a_nested_5xx_status_is_transient(self):
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert compaction_failure_is_transient(
            {"status": {"type": "failed"}, "error": {"cause": {"httpStatusCode": 503}}}
        )

    def test_a_429_is_transient(self):
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert compaction_failure_is_transient({"error": {"httpStatusCode": 429}})

    def test_an_overflowing_conversation_is_not_transient(self):
        """The case the no-retry policy was written for: replaying it repeats
        the same overflow, so it must NOT be classified as retryable."""
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert not compaction_failure_is_transient(
            {"status": {"type": "failed", "reason": "context window exceeded"}}
        )

    def test_the_conversation_summary_cannot_flip_the_verdict(self):
        """Only reason-bearing keys are scanned. conversationSummary is
        backend-echoed, conversation-derived text riding in the very frame the
        KAS branch passes whole, so a summary that merely mentions a timeout must
        not upgrade a permanent overflow to transient — a control decision has to
        be unreachable from content the model wrote."""
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert not compaction_failure_is_transient(
            {
                "kind": "summarization_failed",
                "reason": "context window exceeded",
                "conversationSummary": (
                    "The user asked about a request timeout and rate limit "
                    "handling; we agreed the service unavailable path needs work."
                ),
            }
        )

    def test_a_reason_bearing_key_still_matches_beside_that_summary(self):
        """The scoping must not cost a real case: the same frame with a genuine
        throttle in a reason-bearing field is still transient."""
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert compaction_failure_is_transient(
            {
                "kind": "summarization_failed",
                "conversationSummary": "An unrelated discussion of caching.",
                "cause": {"reason": "MODEL_TEMPORARILY_UNAVAILABLE"},
            }
        )

    def test_a_4xx_is_not_transient(self):
        """A validation or auth failure is answered by fixing the request, not
        by sending it again."""
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert not compaction_failure_is_transient({"error": {"httpStatusCode": 400}})

    def test_a_bare_failure_is_not_transient(self):
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert not compaction_failure_is_transient({"status": {"type": "failed"}})

    def test_a_boolean_is_not_read_as_a_status_code(self):
        """``bool`` is an ``int`` subclass, so a True under this key would
        otherwise compare as 1 — and 1 is not a status code at all."""
        from kiro_crew.acp.client import compaction_failure_is_transient

        assert not compaction_failure_is_transient({"error": {"httpStatusCode": True}})

    def test_the_reason_and_the_verdict_read_the_same_frame(self):
        """Both readers are driven off one walker, so the reason a row DISPLAYS
        and the verdict that decides the RETRY cannot be derived from different
        views of the same frame. Separate calls rather than a paired helper,
        because only the verdict crosses the provider contract."""
        from kiro_crew.acp.client import (
            compaction_failure_detail,
            compaction_failure_is_transient,
        )

        frame = {
            "userFacingSessionErrorMessage": "High traffic — try another model.",
            "cause": {"reason": "MODEL_TEMPORARILY_UNAVAILABLE"},
        }
        assert compaction_failure_detail(frame) == "High traffic — try another model."
        assert compaction_failure_is_transient(frame) is True
