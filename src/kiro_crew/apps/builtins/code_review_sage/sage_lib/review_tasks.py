"""Worker prompts — the task text the review driver hands each pool session.

Split out of ``review_driver`` (which still exposes every name here, and still
looks up ``build_review_task`` / ``build_post_task`` in its own namespace at call
time, so tests patching them on ``review_driver`` keep working) to keep that
module under the file-line cap.
"""

from __future__ import annotations

import sys

from sage_lib import pipeline
from sage_lib.delivery_intent import _operation_marker, _predecessor_instruction


def _confirmed_host(link: str) -> str:
    """The link's validated GitHub host, or ``""`` for a bare legacy change
    token that names no host at all.

    FAILS CLOSED: raises ``AdapterError`` when the link NAMES a host that does
    not (re)validate against ``allowed_hosts()`` — e.g. a GitHub Enterprise
    host removed from ``github_hosts`` between run start and prompt build, or
    an unreadable config. Producing a prompt for such a link would let its
    ``gh api`` calls default to PUBLIC github.com and cross GitHub instances
    (fetching from — or posting an internal enterprise draft onto — a public
    same-slug PR). A token that names no host (``CR-1``) has no instance to
    cross to, so it keeps the legacy default-instructions path: ``""`` here
    means "no host named", never "failed to resolve" — those two cases are
    deliberately NOT allowed to look identical."""
    try:
        return pipeline.adapters.github_pr_ref(link)[0]
    except pipeline.adapters.AdapterError:
        if pipeline.adapters.link_names_a_host(link):
            raise
        return ""


def python_command() -> str:
    """Absolute interpreter the worker must use for the app's ``sage_lib/...`` commands.

    The prompts hand a session on some host shell a command line, so they have to
    name an interpreter that exists there. ``python3`` does not: neither the
    python.org installer nor ``python -m venv`` creates a ``python3.exe`` on
    Windows, where the name instead resolves to a Microsoft Store app-execution
    alias that runs no Python — the command then produces no result record and
    the review yields nothing while looking like it ran. ``sys.executable`` is an
    absolute path to a real interpreter on every platform, so it satisfies that.

    This returns the path RAW, and deliberately does no shell quoting. Quoting
    requires knowing the worker's shell, which is not pinned: on Windows the
    session may get PowerShell or cmd, and the two disagree — a quoted path is a
    string literal needing PowerShell's call operator in one and a syntax error
    with that operator in the other. Since a Windows profile with a space in it
    (``C:\\Users\\First Last\\...``) is ordinary rather than exceptional, guessing
    wrong would restore the same silent no-result failure for a large share of
    Windows users. The worker knows its own shell, so the prompt tells it to
    quote as that shell requires.
    Deliberately NOT the shared ``resolve_app_python(app_root)`` policy, which
    prefers ``<app_root>/.venv``'s interpreter. ``store.app_root()`` resolves under
    ``KIROCREW_HOME`` -- the same writable tree the review worker itself writes into,
    and that worker is prompt-injectable. A worker that planted an executable at
    ``.venv/Scripts/python.exe`` would have the NEXT review execute it: arbitrary
    code execution as the gateway plus forged result records, and persistence into
    a later run rather than a capability it already had. So the interpreter is taken
    from the process the prompt is built IN, never from a path the worker can write.

    Nothing is given up by declining the venv preference here. That preference
    exists so an app's own dependencies are importable, and this app has no
    ``requirements.txt``; the only non-stdlib import anywhere in ``sage_lib`` is
    ``kiro_crew`` itself, which is importable under ``sys.executable`` by
    construction because that is the interpreter running the gateway. An app venv
    would in fact be the weaker choice: it need not have ``kiro_crew`` installed
    at all, which would fail every ``sage_lib`` import instead of just the review.
    """
    return sys.executable


def _fetch_instruction(link: str) -> str:
    """Platform-aware FETCH instruction for the gate/deep prompts (GitHub only),
    carrying the link's confirmed host so the worker pulls the PR from ITS
    instance's API. Fails closed via ``_confirmed_host`` — no prompt is built
    for a link whose host cannot be revalidated."""
    host = _confirmed_host(link)
    try:
        platform = pipeline.adapters.detect_platform(link)
    except Exception:  # a bare legacy token -> default GitHub instructions
        platform = "github"
    # An empty host means the token named NO host at all (legacy "CR-1" ids):
    # those keep the CLI-default flow. A link that NAMES a host — including
    # github.com — gets it pinned explicitly by fetch_spec.
    return pipeline.fetch_spec(platform, host=host)


def build_consolidation_task(
    namespace: str, live_path: str, candidate_path: str, out_path: str
) -> str:
    """Prompt for the one-shot merge that turns staged candidates into the ruleset.

    The judgment is the model's: whether a candidate is already covered, whether
    two rules should collapse into one, and how to phrase the survivor. The
    mechanics are not — the worker WRITES a file and the caller applies it through
    ``learning.consolidate_apply``, which refuses empty content. That split is why
    a bad merge cannot silently wipe the ruleset.
    """
    return (
        "You are consolidating Code Review Sage's learned-pattern ruleset for the "
        f"namespace {namespace!r}. This is a one-shot merge, not a review.\n"
        f"  * CURRENT ruleset (what reviews load today): {live_path}\n"
        f"  * PENDING candidates (staged, not yet used):  {candidate_path}\n"
        "Read both, then write the merged ruleset to "
        f"{out_path}.\n"
        "Rules for the merge:\n"
        "  1. Keep every current pattern unless a candidate genuinely supersedes "
        "it — this file IS the reviewer's memory, so dropping a rule loses a "
        "lesson permanently. Deletion needs a reason you could defend.\n"
        "  2. Collapse duplicates and near-duplicates into ONE sharper rule. "
        "Several candidates often come from the same incident.\n"
        "  3. Each pattern is a single high-level, code-agnostic heuristic: a "
        "title plus one paragraph of guidance. Strip the incident anecdote, the "
        "repo name, the PR number and any code sample — if a rule needs an "
        "example to be understood it is underspecified, so sharpen the wording "
        "instead.\n"
        "  4. Preserve the exact on-disk format, one block per pattern:\n"
        "     ### <title> <!-- scope:common --> <!-- impact:high|medium|low --> "
        "<!-- added:<ISO8601Z> -->\n"
        "     <one paragraph of guidance on a single line>\n"
        "  5. Keep the file's leading markdown header. Write NOTHING else to the "
        "file — no commentary, no fences.\n"
        "Report in your final message how many patterns you kept, merged and "
        "dropped, and why anything was dropped."
    )


def build_review_task(change_link: str) -> str:
    """Single-pass review prompt: ONE isolated session does the WHOLE review —
    design reasoning AND every code-level dimension — in a single turn, and writes
    the complete result record (phase1 design fields + findings + counts +
    ship_summary + a coverage signal). Design is one dimension of the review, not a
    separate gated stage; the driver runs neither a gate turn nor a convergence
    loop. The session RECORDS findings only — it never posts (the driver builds the
    Python-redacted bodies and a separate poster publishes them verbatim)."""
    py = python_command()
    return (
        "You are a Code Review Sage reviewer running in an ISOLATED, CLEAN session. "
        "Do the COMPLETE review of EXACTLY ONE change in a SINGLE thorough pass: "
        + change_link
        + ". There is NO separate gate and NO follow-up round — cover "
        "everything now, carefully, at maximum thinking effort.\n"
        "Run every `sage_lib/...` command — the ones below AND the ones the skill "
        "writes as `<python> ...` — with this interpreter: `" + py + "`. Use that "
        "absolute path verbatim (quote it as YOUR shell requires if it contains "
        "spaces); do NOT substitute `python3` or `python`.\n"
        "Load the `sage-review` skill and follow its per-change review ruleset:\n"
        "  1. Self-heal the store; load patterns from active namespaces "
        "(`" + py + " sage_lib/learning.py list-for-review`).\n"
        "  2. Resolve the per-repo rule pack (if any) and apply it as additional rules.\n"
        "  3. Fetch the change — " + _fetch_instruction(change_link) + " — and "
        "normalize via `"
        + py
        + " sage_lib/pipeline.py prepare --link "
        + change_link
        + " --payload-file <file>`.\n"
        "  4. DESIGN dimension (THINK DEEPLY — highest leverage): work the change "
        "through the skill's `Deep design reasoning` lenses (architectural fit, "
        "contract/data evolution, alternatives & proportionality, failure modes, "
        "root-cause vs symptom) as consequence chains; the weakest applicable lens "
        "sets design_risk. Produce gate_verdict (PASS|CONCERNS|BLOCK — BLOCK is ONLY "
        "for a genuine DESIGN defect: no real problem, wrong/over-engineered fix, or a "
        "clearly better alternative ignored; a large blast radius / high criticality "
        "is NEVER on its own a BLOCK), design_risk, criticality, and — ONLY on "
        "CONCERNS/BLOCK — a straightforward, direct design_headline (issue + "
        "recommended direction, no hedging; empty on PASS), plus problem (one "
        "sentence), why_it_matters (one or two SHORT lines), and solution_assessment "
        "(a few 'Label: text' facets on SEPARATE LINES).\n"
        "  5. CODE dimensions: walk EVERY changed hunk against ALL 9 code-level "
        "dimensions + self-critique (Filter/Merge/Sharpen/Stabilize) -> surviving "
        "🔴/🟡 findings. Severity three-tier: 🔴 must-fix (breaks now OR a latent "
        "high-probability/high-impact 'have-to-fix' — do NOT downgrade to 🟡 just "
        "because it works today); 🟡 should-fix; drop nice-to-haves. Keep first-class: "
        "STRICT bidirectional description<->diff fidelity (no phantom claims, no "
        "undocumented change) and an explicit threat chain on every security finding "
        "(entry point -> trust boundary -> exploit -> impact). A design CONCERNS/BLOCK "
        "is ALSO expressed as a finding so it reaches the author.\n"
        "  6. COVERAGE self-check (the driver relies on this): before emitting, "
        "enumerate every changed FILE and confirm you reviewed each against all "
        "dimensions. Set `files_covered` to the list of changed file paths you "
        "actually reviewed, and `coverage_complete` to true ONLY if that list covers "
        "every changed file — otherwise set it false (the driver will run ONE "
        "targeted follow-up on the remainder). Do not pad the list; report honestly.\n"
        "  7. RECORD ONLY — do NOT post any comments. Write data/results/<id>.json: "
        "phase1 (gate_verdict, design_risk, criticality, design_headline, problem, "
        "why_it_matters, solution_assessment) + blast_radius; `findings` (each with "
        "file, line, severity 🔴/🟡, dimension, headline, observation, consequence, "
        "suggestion, snippet, lang); `counts` {red,yellow}; `ship_summary` (ONE straightforward "
        "line: good-to-ship + reason when there are no 🔴, or not-ready + the "
        "must-fix/design reason otherwise); `files_covered`; `coverage_complete`; "
        "deep_reviewed=true. The driver builds the redacted bodies and a separate "
        "poster publishes them — you MUST NOT call any comment tool.\n"
        "     `headline` is the finding's CONCLUSION in ONE sentence under about 100 "
        "characters: what is actually wrong, stated directly. It is the only line a "
        "reader is guaranteed to see, so it must stand alone — no hedging, no leading "
        "severity word, no file path or line number (those are separate fields), and "
        "not merely the first sentence of `observation` restated. `observation` then "
        "carries the evidence, `consequence` the chain of harm, `suggestion` the fix.\n"
        "  8. If this change is itself a FIX (is_fix), run INLINE miss-analysis "
        "(learn-from-sage): trace the introducing change, ask which dimension was "
        "blind, and STAGE the learning "
        "(`" + py + " sage_lib/learning.py stage --file <pattern.json> --source fix_introduce`) "
        "— NOT applied to the live ruleset until a human consolidates.\n"
        "Do NOT spawn further subagents. Execute; do not ask questions."
    )


def build_review_followup_task(change_link: str) -> str:
    """Bounded coverage backstop — dispatched AT MOST ONCE, and only when the single
    review reported ``coverage_complete=false``. It reviews the STILL-UNCOVERED
    changed files and APPENDS only net-new findings (never repeats/removes existing
    ones), then marks coverage complete. It runs at most one targeted pass,
    signal-driven, not count-delta-driven."""
    py = python_command()
    return (
        "You are a Code Review Sage reviewer running in an ISOLATED, CLEAN session. "
        "A prior pass reviewed EXACTLY ONE change: " + change_link + " but reported "
        "INCOMPLETE file coverage (coverage_complete=false) in data/results/<id>.json.\n"
        "Run every `sage_lib/...` command — the ones below AND the ones the skill "
        "writes as `<python> ...` — with this interpreter: `" + py + "`. Use that "
        "absolute path verbatim (quote it as YOUR shell requires if it contains "
        "spaces); do NOT substitute `python3` or `python`.\n"
        "Load the `sage-review` skill and follow its per-change review ruleset:\n"
        "  1. Self-heal the store; load patterns "
        "(`" + py + " sage_lib/learning.py list-for-review`).\n"
        "  2. Resolve the per-repo rule pack (if any) and apply it as additional rules.\n"
        "  3. Fetch the change — " + _fetch_instruction(change_link) + " — and "
        "normalize via `"
        + py
        + " sage_lib/pipeline.py prepare --link "
        + change_link
        + " --payload-file <file>`. READ the existing record: its `findings` and "
        "`files_covered`.\n"
        "  4. Review ONLY the changed files NOT already in `files_covered`, against "
        "ALL 9 code dimensions AND the design lenses, with the same three-tier "
        "severity (🔴/🟡, drop nice-to-haves) and the description<->diff fidelity + "
        "security threat-chain checks.\n"
        "  5. RECORD ONLY — APPEND only NET-NEW findings (do NOT repeat, reword, or "
        "remove any already-recorded finding); each carries the SAME fields as the "
        "first pass (file, line, severity, dimension, headline, observation, "
        "consequence, suggestion, snippet, lang), where `headline` is the finding's "
        "conclusion in ONE sentence under about 100 characters; recompute `counts` "
        "{red,yellow} over "
        "the FULL list; refresh `ship_summary`; extend `files_covered` to include "
        "every changed file and set `coverage_complete=true`; keep deep_reviewed=true "
        "and PRESERVE the phase1 block. You MUST NOT call any comment tool.\n"
        "Do NOT spawn further subagents. Execute; do not ask questions."
    )


def build_post_task(
    change_link: str,
    operation_id: str = "",
    predecessor: dict | None = None,
    *,
    recovering: bool = False,
) -> str:
    """Poster prompt: publish the driver-built, Python-REDACTED DRAFT comments for
    one change. The bodies are authoritative and already scrubbed in Python — the
    poster posts them VERBATIM and only resolves the (non-sensitive) anchor. This
    is what makes PR-surface redaction deterministic (security-controls): no LLM
    free-text reaches the PR, because the LLM never composes a posted body."""
    _preamble = (
        "You are a Code Review Sage poster running in an ISOLATED, CLEAN session. "
        "Your ONLY job: publish pre-built, pre-redacted DRAFT review comments for "
        "EXACTLY ONE change: " + change_link + ". The comment bodies are AUTHORITATIVE "
        "and already redacted in Python — post each one VERBATIM. Do NOT compose, edit, "
        "summarize, truncate, translate, or add to any body.\n"
    )
    # FAIL CLOSED on host resolution — the host decides which GitHub instance
    # every `gh api` call in this prompt targets. `_confirmed_host` raises when
    # the link names a host that fails revalidation (a GHE host absent from
    # `github_hosts`, an unreadable config); producing a prompt then
    # would let every call default to PUBLIC github.com and post an internal
    # enterprise draft onto a public same-slug PR. The raise is converted to a
    # per-change post failure by `post_recorded`. An empty host means the token
    # named NO host at all (legacy "CR-1" ids) — never a failed resolution.
    host = _confirmed_host(change_link)
    if host:
        # ALWAYS name the host — including github.com — so the poster's gh
        # calls can never drift to the CLI's configured default instance.
        _preamble += (
            f"This PR lives on the GitHub host `{host}`: add "
            f"`--hostname {host}` to EVERY `gh api` call below.\n"
        )
    # GitHub's draft is a PENDING review: ONE API call carrying all inline
    # comments + a body, created WITHOUT an `event` key so it is NOT submitted.
    # The envelope is pre-built + redacted in Python (`github_review_payload`);
    # the poster posts it verbatim and never submits. A HUMAN submits it.
    return (
        _preamble + "  1. Read data/results/<id>.json and take its `github_review_payload` "
        "object (fields: body, comments[], optional commit_id). It was assembled "
        "AND redacted in Python — use it EXACTLY as given; do NOT rebuild it. Parse "
        "<owner>/<repo>/<number> from the PR URL. Add this exact hidden HTML comment "
        "on its own final line, preceded by a newline, at the END of the outbound "
        "`body` in the TEMP JSON only: `"
        + _operation_marker(operation_id)
        + "`. Do not write the marker back to the "
        "result record or alter any other payload content.\n"
        + _predecessor_instruction(predecessor, recovering=recovering)
        + "  3. THEN write that outbound payload to a temp JSON file and create ONE "
        "PENDING (unsubmitted) review:\n"
        "     gh api --method POST repos/<owner>/<repo>/pulls/<number>/reviews "
        "--input <tmpfile>\n"
        "     The payload has NO `event` key, so GitHub creates the review as "
        "PENDING — it is NOT submitted and only YOU can see it until a HUMAN "
        "submits it in the GitHub UI. You MUST NOT add an `event` field, MUST NOT "
        "call any submit/approve/dismiss endpoint, and MUST NOT run `gh pr review` "
        "(that would submit immediately). `gh` uses its own stored auth — never "
        "read, print, or pass any token.\n"
        "  4. Report the number of payload units actually posted by updating ONLY "
        "`posted_comments` in data/results/<id>.json: len(comments) plus 1 when "
        "the original payload body is non-empty. Do NOT modify any other field, "
        "including delivery_intent, findings, phase1, pending_comments, or "
        "github_review_payload. This count is advisory; the caller records "
        "delivery evidence only after its own GitHub read-back.\n"
        "Do NOT spawn further subagents. Execute; do not ask questions."
    )
