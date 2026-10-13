"""Validation for the persisted ``session_environments`` config section.

The ONE source both the loader (``SessionEnvironmentsConfig.from_raw``) and the
dashboard write gate (``PATCH /api/config/kirocrew``) use, so what a save accepts
and what a load keeps cannot drift apart.

The section stores CONFIGURATION ONLY -- a Coder deployment URL, workspace
profiles and Fargate task/network references. Nothing here registers a provider,
contacts a provider, or provisions anything. It carries no credentials.

Two entry points per shape:

* ``validate_*`` raises :class:`SessionEnvironmentsError` naming the offending
  field -- the API gate turns that into a 400 and nothing is written.
* :func:`load_session_environments` is the lenient load-time twin: an invalid
  ``coder_url`` resolves to ``""`` and an invalid or duplicate profile is dropped
  (with a warning), so one bad entry in a hand-edited file never discards the
  valid ones beside it.

This module imports nothing from the config package, so ``sections.py`` can depend
on it without an import cycle.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("kiro_crew.config.loader")

#: Most profiles one config may hold.
MAX_PROFILES = 20
#: Profile id grammar: ASCII alphanumeric first, then alphanumerics, ``_`` or ``-``.
PROFILE_ID_MAX_LEN = 64
PROFILE_NAME_MAX_LEN = 128
PROFILE_TEMPLATE_MAX_LEN = 255
PROFILE_WORKDIR_MAX_LEN = 1024
CODER_URL_MAX_LEN = 2048

PROFILE_KEYS: tuple[str, ...] = ("id", "name", "template", "workdir")
#: Optional profile keys: absent means the default, so older saved configs and
#: older dashboards that never send them stay valid.
PROFILE_OPTIONAL_KEYS: tuple[str, ...] = ("hot_pool",)
#: Most ready workspaces one profile may keep waiting. Each one is a running
#: MicroVM billed while it waits, so the ceiling is deliberately small.
HOT_POOL_MAX = 5
SECTION_KEYS: tuple[str, ...] = ("coder_url", "profiles")
#: Optional section keys. An older dashboard that omits one keeps the saved
#: value: the locked writer in ``dashboard/handlers/core.py`` preserves it.
SECTION_OPTIONAL_KEYS: tuple[str, ...] = (
    "fargate_profiles",
    "sprites_profiles",
    "boxd_profiles",
    "kiro_cloud_profiles",
    "archived_retention_days",
    "default_profile",
    "stop_on_shutdown",
)
#: Days an archived session's stopped workspace is kept before it is deleted.
#: A stopped workspace still holds its disk, so the ceiling is a week.
ARCHIVED_RETENTION_DAYS_MAX = 7
ARCHIVED_RETENTION_DAYS_DEFAULT = 7

_PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,%d}" % (PROFILE_ID_MAX_LEN - 1))
# ASCII control characters: never meaningful in a label, template name or path,
# and a NUL in a path is refused by every OS call that would later use it.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class SessionEnvironmentsError(ValueError):
    """A ``session_environments`` value failed validation; the message names the field."""


def validate_coder_url(value: object) -> str:
    """Return the normalized Coder URL, or raise :class:`SessionEnvironmentsError`.

    ``""`` means "not configured". Otherwise an ``http``/``https`` URL with a
    hostname and no userinfo, query or fragment. Surrounding whitespace is
    stripped; the URL is not contacted.
    """
    if not isinstance(value, str):
        raise SessionEnvironmentsError("coder_url must be a string")
    url = value.strip()
    if not url:
        return ""
    if len(url) > CODER_URL_MAX_LEN:
        raise SessionEnvironmentsError(f"coder_url must be at most {CODER_URL_MAX_LEN} characters")
    if _CONTROL_RE.search(url) or " " in url:
        raise SessionEnvironmentsError("coder_url must not contain spaces or control characters")
    try:
        parts = urlsplit(url)
        # ``.port`` raises ValueError on a non-numeric or out-of-range port.
        _ = parts.port
    except ValueError:
        raise SessionEnvironmentsError("coder_url must be a valid URL") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise SessionEnvironmentsError("coder_url must be an http or https URL")
    if not parts.hostname:
        raise SessionEnvironmentsError("coder_url must include a hostname")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise SessionEnvironmentsError("coder_url must not include a username or password")
    # Test the raw characters, not only the parsed parts: ``https://h?`` parses
    # to an EMPTY query, and a bare ``?``/``#`` is still not a plain base URL.
    if "?" in url or parts.query:
        raise SessionEnvironmentsError("coder_url must not include a query string")
    if "#" in url or parts.fragment:
        raise SessionEnvironmentsError("coder_url must not include a fragment")
    return url


def _bounded_text(value: object, field: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise SessionEnvironmentsError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise SessionEnvironmentsError(f"{field} must not be empty")
    if len(text) > max_len:
        raise SessionEnvironmentsError(f"{field} must be at most {max_len} characters")
    if _CONTROL_RE.search(text):
        raise SessionEnvironmentsError(f"{field} must not contain control characters")
    return text


def validate_hot_pool(value: object, where: str = "hot_pool") -> int:
    """Number of ready workspaces to keep for a profile: an int in 0..HOT_POOL_MAX."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SessionEnvironmentsError(f"{where} must be a whole number")
    if not 0 <= value <= HOT_POOL_MAX:
        raise SessionEnvironmentsError(f"{where} must be between 0 and {HOT_POOL_MAX}")
    return value


def validate_archived_retention_days(value: object) -> int:
    """Days to keep an archived session's workspace: an int in 0..7 (0 deletes it now)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise SessionEnvironmentsError("archived_retention_days must be an integer")
    if not 0 <= value <= ARCHIVED_RETENTION_DAYS_MAX:
        raise SessionEnvironmentsError(
            f"archived_retention_days must be between 0 and {ARCHIVED_RETENTION_DAYS_MAX}"
        )
    return value


def load_archived_retention_days(raw: object) -> int:
    """Lenient load: the saved value, or the default when absent or invalid."""
    if not isinstance(raw, dict) or "archived_retention_days" not in raw:
        return ARCHIVED_RETENTION_DAYS_DEFAULT
    try:
        return validate_archived_retention_days(raw["archived_retention_days"])
    except SessionEnvironmentsError as exc:
        logger.warning("session_environments: %s; using %d", exc, ARCHIVED_RETENTION_DAYS_DEFAULT)
        return ARCHIVED_RETENTION_DAYS_DEFAULT


def validate_stop_on_shutdown(value: object) -> bool:
    """Whether the gateway stops its remote environments when it shuts down."""
    if not isinstance(value, bool):
        raise SessionEnvironmentsError("stop_on_shutdown must be true or false")
    return value


def load_stop_on_shutdown(raw: object) -> bool:
    """Lenient load: the saved choice, or True (stop them) when absent or invalid."""
    if not isinstance(raw, dict) or "stop_on_shutdown" not in raw:
        return True
    try:
        return validate_stop_on_shutdown(raw["stop_on_shutdown"])
    except SessionEnvironmentsError as exc:
        logger.warning("session_environments: %s; stopping them on shutdown", exc)
        return True


def validate_default_profile(value: object) -> str:
    """The profile id new dashboard sessions start on; "" is this machine.

    Shape only here: that the id names a saved profile is checked against the
    whole section (``validate_session_environments``) and again at bind time,
    where a dangling id falls back to this machine.
    """
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not _PROFILE_ID_RE.fullmatch(value):
        raise SessionEnvironmentsError("default_profile must be a profile id or empty")
    return value


def load_default_profile(raw: object) -> str:
    """Lenient load: the saved id, or "" (this machine) when absent or invalid."""
    if not isinstance(raw, dict) or "default_profile" not in raw:
        return ""
    try:
        return validate_default_profile(raw["default_profile"])
    except SessionEnvironmentsError as exc:
        logger.warning("session_environments: %s; new sessions run on this machine", exc)
        return ""


def validate_profile(value: object, index: int = 0, *, where: str = "") -> dict[str, Any]:
    """Return one normalized profile record, or raise :class:`SessionEnvironmentsError`.

    Strict shape: the keys ``id``, ``name``, ``template`` and ``workdir``, plus
    the optional ``hot_pool`` (ready workspaces to keep, default 0). An unknown
    key is refused rather than silently dropped, so a caller that thinks it
    stored something (a credential, say) learns it did not. ``where`` names the
    list the entry came from in messages (another provider's list reuses this
    check), so the person is pointed at the profile that is actually wrong.
    """
    where = where or f"profiles[{index}]"
    if not isinstance(value, dict):
        raise SessionEnvironmentsError(f"{where} must be an object")
    unknown = sorted(str(k) for k in value if k not in (*PROFILE_KEYS, *PROFILE_OPTIONAL_KEYS))
    if unknown:
        raise SessionEnvironmentsError(f"{where} has unknown key(s): {unknown}")
    missing = [k for k in PROFILE_KEYS if k not in value]
    if missing:
        raise SessionEnvironmentsError(f"{where} is missing key(s): {missing}")

    profile_id = value["id"]
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
        raise SessionEnvironmentsError(
            f"{where}.id must start with an ASCII letter or digit and contain only "
            f"letters, digits, '_' or '-' (at most {PROFILE_ID_MAX_LEN} characters)"
        )
    name = _bounded_text(value["name"], f"{where}.name", PROFILE_NAME_MAX_LEN)
    template = _bounded_text(value["template"], f"{where}.template", PROFILE_TEMPLATE_MAX_LEN)
    workdir = _bounded_text(value["workdir"], f"{where}.workdir", PROFILE_WORKDIR_MAX_LEN)
    if not workdir.startswith("/"):
        raise SessionEnvironmentsError(f"{where}.workdir must be an absolute POSIX path")
    hot_pool = validate_hot_pool(value.get("hot_pool", 0), f"{where}.hot_pool")
    return {
        "id": profile_id,
        "name": name,
        "template": template,
        "workdir": workdir,
        "hot_pool": hot_pool,
    }


def validate_profiles(value: object) -> list[dict[str, Any]]:
    """Return the normalized profile list, or raise :class:`SessionEnvironmentsError`."""
    if not isinstance(value, list):
        raise SessionEnvironmentsError("profiles must be a list")
    if len(value) > MAX_PROFILES:
        raise SessionEnvironmentsError(f"profiles must hold at most {MAX_PROFILES} entries")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        profile = validate_profile(entry, index)
        if profile["id"] in seen:
            raise SessionEnvironmentsError(f"profiles[{index}].id {profile['id']!r} is a duplicate")
        seen.add(profile["id"])
        out.append(profile)
    return out


def validate_session_environments(value: object) -> dict[str, Any]:
    """Return the normalized whole section, or raise :class:`SessionEnvironmentsError`.

    Written atomically. Both Coder keys are required; ``fargate_profiles`` is
    optional for older clients, with omission preserved by the locked writer.
    Other keys are refused.
    """
    if not isinstance(value, dict):
        raise SessionEnvironmentsError("session_environments must be an object")
    unknown = sorted(str(k) for k in value if k not in (*SECTION_KEYS, *SECTION_OPTIONAL_KEYS))
    if unknown:
        raise SessionEnvironmentsError(f"session_environments has unknown key(s): {unknown}")
    missing = [k for k in SECTION_KEYS if k not in value]
    if missing:
        raise SessionEnvironmentsError(f"session_environments is missing key(s): {missing}")
    result: dict[str, Any] = {
        "coder_url": validate_coder_url(value["coder_url"]),
        "profiles": validate_profiles(value["profiles"]),
    }
    # Older Coder-only clients omit this list. The locked dashboard writer
    # preserves its current value rather than treating omission as deletion.
    if "fargate_profiles" in value:
        result["fargate_profiles"] = validate_fargate_profiles(value["fargate_profiles"])
    if "sprites_profiles" in value:
        result["sprites_profiles"] = validate_sprites_profiles(value["sprites_profiles"])
    if "boxd_profiles" in value:
        result["boxd_profiles"] = validate_boxd_profiles(value["boxd_profiles"])
    if "kiro_cloud_profiles" in value:
        result["kiro_cloud_profiles"] = validate_kiro_cloud_profiles(value["kiro_cloud_profiles"])
    # A session's choice records only a profile id, and that id decides the
    # provider, so one id may not name profiles of two providers at once.
    seen: dict[str, str] = {}
    for key, provider in (
        ("profiles", "Coder"),
        ("fargate_profiles", "Fargate"),
        ("sprites_profiles", "Sprites"),
        ("boxd_profiles", "boxd"),
        ("kiro_cloud_profiles", "Kiro Cloud"),
    ):
        for profile in result.get(key, []):
            other = seen.setdefault(profile["id"], provider)
            if other != provider:
                raise SessionEnvironmentsError(
                    f"profile id {profile['id']!r} is used by both a {other} and a "
                    f"{provider} profile"
                )
    if "archived_retention_days" in value:
        result["archived_retention_days"] = validate_archived_retention_days(
            value["archived_retention_days"]
        )
    if "default_profile" in value:
        default = validate_default_profile(value["default_profile"])
        # Only an id this save keeps: a removed profile cannot stay the default.
        if default and default not in seen:
            raise SessionEnvironmentsError(
                f"default_profile {default!r} is not one of the saved profiles"
            )
        result["default_profile"] = default
    if "stop_on_shutdown" in value:
        result["stop_on_shutdown"] = validate_stop_on_shutdown(value["stop_on_shutdown"])
    return result


def load_session_environments(raw: object) -> tuple[str, list[dict[str, Any]]]:
    """Lenient load-time normalization: ``(coder_url, profiles)``, never raises.

    A non-object section, an invalid URL, and each invalid, duplicate or
    over-cap profile fall back individually (``""`` / dropped) with a warning.
    """
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("session_environments must be an object; using defaults")
        return "", []

    coder_url = ""
    if "coder_url" in raw:
        try:
            coder_url = validate_coder_url(raw["coder_url"])
        except SessionEnvironmentsError as exc:
            logger.warning("session_environments: %s; ignoring it", exc)

    profiles: list[dict[str, Any]] = []
    raw_profiles = raw.get("profiles", [])
    if not isinstance(raw_profiles, list):
        logger.warning("session_environments: profiles must be a list; ignoring it")
        raw_profiles = []
    seen: set[str] = set()
    for index, entry in enumerate(raw_profiles):
        if len(profiles) >= MAX_PROFILES:
            logger.warning(
                "session_environments: more than %d profiles; ignoring the rest", MAX_PROFILES
            )
            break
        try:
            profile = validate_profile(entry, index)
        except SessionEnvironmentsError as exc:
            logger.warning("session_environments: %s; dropping that profile", exc)
            continue
        if profile["id"] in seen:
            logger.warning(
                "session_environments: profiles[%d].id %r is a duplicate; dropping it",
                index,
                profile["id"],
            )
            continue
        seen.add(profile["id"])
        profiles.append(profile)
    return coder_url, profiles


FARGATE_PROFILE_KEYS = (
    "id",
    "name",
    "aws_profile",
    "region",
    "cluster",
    "task_definition",
    "subnets",
    "security_groups",
    "assign_public_ip",
    "workdir",
)


def validate_fargate_profile(value: object, index: int = 0) -> dict[str, Any]:
    """Validate saved ECS placement, not permission or runtime readiness.

    References are checked syntactically only. No AWS call, credential lookup,
    task registration or infrastructure mutation is performed by a config save.
    """
    where = f"fargate_profiles[{index}]"
    if not isinstance(value, dict) or set(value) != set(FARGATE_PROFILE_KEYS):
        raise SessionEnvironmentsError(f"{where} must contain exactly {FARGATE_PROFILE_KEYS}")
    common = validate_profile(
        {
            "id": value["id"],
            "name": value["name"],
            "template": "fargate",
            "workdir": value["workdir"],
        },
        index,
        where=where,
    )
    result: dict[str, Any] = {key: common[key] for key in ("id", "name", "workdir")}
    for key, limit in (
        ("aws_profile", 128),
        ("region", 64),
        ("cluster", 2048),
        ("task_definition", 2048),
    ):
        result[key] = _bounded_text(value[key], f"{where}.{key}", limit)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", result["aws_profile"]):
        raise SessionEnvironmentsError(f"{where}.aws_profile must be an AWS profile name")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", result["region"]):
        raise SessionEnvironmentsError(f"{where}.region must be an AWS region name")
    # ECS names or ARNs only, never CLI flags, URLs or command fragments.
    for key in ("cluster", "task_definition"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_:/.-]*", result[key]):
            raise SessionEnvironmentsError(f"{where}.{key} must be an ECS name or ARN")
    for key, prefix, maximum in (("subnets", "subnet", 16), ("security_groups", "sg", 5)):
        items = value[key]
        if not isinstance(items, list) or not 1 <= len(items) <= maximum:
            raise SessionEnvironmentsError(f"{where}.{key} must hold 1-{maximum} IDs")
        if any(
            not isinstance(item, str)
            or not re.fullmatch(rf"{prefix}-(?:[a-f0-9]{{8}}|[a-f0-9]{{17}})", item)
            for item in items
        ):
            raise SessionEnvironmentsError(f"{where}.{key} contains an invalid ID")
        if len(set(items)) != len(items):
            raise SessionEnvironmentsError(f"{where}.{key} contains duplicate IDs")
        result[key] = list(items)
    if not isinstance(value["assign_public_ip"], bool):
        raise SessionEnvironmentsError(f"{where}.assign_public_ip must be a boolean")
    result["assign_public_ip"] = value["assign_public_ip"]
    return result


def validate_fargate_profiles(value: object) -> list[dict[str, Any]]:
    """Strict atomic list validation for the Fargate configuration editor."""
    if not isinstance(value, list) or len(value) > MAX_PROFILES:
        raise SessionEnvironmentsError(
            f"fargate_profiles must be a list of at most {MAX_PROFILES} entries"
        )
    result = [validate_fargate_profile(item, index) for index, item in enumerate(value)]
    if len({item["id"] for item in result}) != len(result):
        raise SessionEnvironmentsError("fargate_profiles contains duplicate profile IDs")
    return result


def load_fargate_profiles(raw: object) -> list[dict[str, Any]]:
    """Keep valid saved entries individually without weakening write validation."""
    if not isinstance(raw, dict):
        return []
    items = raw.get("fargate_profiles", [])
    if not isinstance(items, list):
        logger.warning("session_environments: fargate_profiles must be a list; ignoring it")
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if len(result) >= MAX_PROFILES:
            break
        try:
            profile = validate_fargate_profile(item, index)
        except SessionEnvironmentsError:
            # No raw input or values in diagnostics: malformed hand edits may
            # contain credentials even though the supported schema has none.
            logger.warning("session_environments: dropping invalid Fargate profile %d", index)
            continue
        if profile["id"] not in seen:
            result.append(profile)
            seen.add(profile["id"])
    return result


SPRITES_PROFILE_KEYS = ("id", "name", "token_secret", "workdir")
#: Default vault secret name for a Sprites API token (``<org>/<id>/<secret>``).
SPRITES_TOKEN_SECRET_DEFAULT = "SPRITES_TOKEN"
_SECRET_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def validate_sprites_profile(value: object, index: int = 0) -> dict[str, Any]:
    """Validate a saved Fly.io Sprites profile. No API call, no token read.

    The token itself is never part of the profile: ``token_secret`` names the
    gateway vault entry (Settings > Secrets) that holds it.
    """
    where = f"sprites_profiles[{index}]"
    if not isinstance(value, dict) or set(value) != set(SPRITES_PROFILE_KEYS):
        raise SessionEnvironmentsError(f"{where} must contain exactly {SPRITES_PROFILE_KEYS}")
    common = validate_profile(
        {
            "id": value["id"],
            "name": value["name"],
            "template": "sprites",
            "workdir": value["workdir"],
        },
        index,
        where=where,
    )
    result: dict[str, Any] = {key: common[key] for key in ("id", "name", "workdir")}
    token_secret = value["token_secret"]
    if not isinstance(token_secret, str) or not _SECRET_NAME_RE.fullmatch(token_secret):
        raise SessionEnvironmentsError(f"{where}.token_secret must be a secret name")
    result["token_secret"] = token_secret
    return result


def validate_sprites_profiles(value: object) -> list[dict[str, Any]]:
    """Strict atomic list validation for the Sprites configuration editor."""
    if not isinstance(value, list) or len(value) > MAX_PROFILES:
        raise SessionEnvironmentsError(
            f"sprites_profiles must be a list of at most {MAX_PROFILES} entries"
        )
    result = [validate_sprites_profile(item, index) for index, item in enumerate(value)]
    if len({item["id"] for item in result}) != len(result):
        raise SessionEnvironmentsError("sprites_profiles contains duplicate profile IDs")
    return result


def load_sprites_profiles(raw: object) -> list[dict[str, Any]]:
    """Keep valid saved entries individually without weakening write validation."""
    if not isinstance(raw, dict):
        return []
    items = raw.get("sprites_profiles", [])
    if not isinstance(items, list):
        logger.warning("session_environments: sprites_profiles must be a list; ignoring it")
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if len(result) >= MAX_PROFILES:
            break
        try:
            profile = validate_sprites_profile(item, index)
        except SessionEnvironmentsError:
            logger.warning("session_environments: dropping invalid Sprites profile %d", index)
            continue
        if profile["id"] not in seen:
            result.append(profile)
            seen.add(profile["id"])
    return result


BOXD_PROFILE_KEYS = ("id", "name", "snapshot", "vcpu", "workdir")
#: 0 is the org's default size; the others are boxd's own vCPU sizes.
BOXD_VCPU_CHOICES = (0, 1, 2, 4, 8)
_SNAPSHOT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def validate_boxd_profile(value: object, index: int = 0) -> dict[str, Any]:
    """Validate a saved boxd profile. No CLI call, no credential.

    The gateway machine's own ``boxd auth login`` authenticates, so a profile
    names only what to create: an optional snapshot to start from, a size, and
    the working directory.
    """
    where = f"boxd_profiles[{index}]"
    if not isinstance(value, dict) or set(value) != set(BOXD_PROFILE_KEYS):
        raise SessionEnvironmentsError(f"{where} must contain exactly {BOXD_PROFILE_KEYS}")
    common = validate_profile(
        {"id": value["id"], "name": value["name"], "template": "boxd", "workdir": value["workdir"]},
        index,
        where=where,
    )
    result: dict[str, Any] = {key: common[key] for key in ("id", "name", "workdir")}
    snapshot = value["snapshot"]
    if not isinstance(snapshot, str) or (snapshot and not _SNAPSHOT_RE.fullmatch(snapshot)):
        raise SessionEnvironmentsError(f"{where}.snapshot must be a snapshot name or id, or empty")
    vcpu = value["vcpu"]
    if isinstance(vcpu, bool) or vcpu not in BOXD_VCPU_CHOICES:
        raise SessionEnvironmentsError(f"{where}.vcpu must be one of {BOXD_VCPU_CHOICES}")
    if snapshot and vcpu:
        # boxd keeps a snapshot's own size; naming one there is an error.
        raise SessionEnvironmentsError(f"{where}: a snapshot keeps its own size; set vcpu to 0")
    result["snapshot"] = snapshot
    result["vcpu"] = vcpu
    return result


def validate_boxd_profiles(value: object) -> list[dict[str, Any]]:
    """Strict atomic list validation for the boxd configuration editor."""
    if not isinstance(value, list) or len(value) > MAX_PROFILES:
        raise SessionEnvironmentsError(
            f"boxd_profiles must be a list of at most {MAX_PROFILES} entries"
        )
    result = [validate_boxd_profile(item, index) for index, item in enumerate(value)]
    if len({item["id"] for item in result}) != len(result):
        raise SessionEnvironmentsError("boxd_profiles contains duplicate profile IDs")
    return result


def load_boxd_profiles(raw: object) -> list[dict[str, Any]]:
    """Keep valid saved entries individually without weakening write validation."""
    if not isinstance(raw, dict):
        return []
    items = raw.get("boxd_profiles", [])
    if not isinstance(items, list):
        logger.warning("session_environments: boxd_profiles must be a list; ignoring it")
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if len(result) >= MAX_PROFILES:
            break
        try:
            profile = validate_boxd_profile(item, index)
        except SessionEnvironmentsError:
            logger.warning("session_environments: dropping invalid boxd profile %d", index)
            continue
        if profile["id"] not in seen:
            result.append(profile)
            seen.add(profile["id"])
    return result


KIRO_CLOUD_PROFILE_KEYS = ("id", "name", "repositories")
#: Most repositories one Kiro Cloud session opens.
KIRO_CLOUD_REPOSITORIES_MAX = 10
#: ``name`` or ``owner/name``, the forms kiro-cli's ``--repo`` takes.
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(?:/[A-Za-z0-9_.-]{1,100})?")


def validate_kiro_cloud_profile(value: object, index: int = 0) -> dict[str, Any]:
    """Validate a saved Kiro Cloud Sessions profile. No KAS call, no credential.

    The gateway's own Kiro sign-in authenticates. A profile names only the
    repositories the cloud sandbox opens; an empty list opens an empty workspace.
    """
    where = f"kiro_cloud_profiles[{index}]"
    if not isinstance(value, dict) or set(value) != set(KIRO_CLOUD_PROFILE_KEYS):
        raise SessionEnvironmentsError(f"{where} must contain exactly {KIRO_CLOUD_PROFILE_KEYS}")
    common = validate_profile(
        {"id": value["id"], "name": value["name"], "template": "kiro-cloud", "workdir": "/"},
        index,
        where=where,
    )
    repositories = value["repositories"]
    if not isinstance(repositories, list) or len(repositories) > KIRO_CLOUD_REPOSITORIES_MAX:
        raise SessionEnvironmentsError(
            f"{where}.repositories must be a list of at most {KIRO_CLOUD_REPOSITORIES_MAX} entries"
        )
    repos: list[str] = []
    for repo in repositories:
        if not isinstance(repo, str) or not _REPOSITORY_RE.fullmatch(repo.strip()):
            raise SessionEnvironmentsError(
                f"{where}.repositories entries must be a repository name or owner/name"
            )
        if repo.strip() not in repos:
            repos.append(repo.strip())
    return {"id": common["id"], "name": common["name"], "repositories": repos}


def validate_kiro_cloud_profiles(value: object) -> list[dict[str, Any]]:
    """Strict atomic list validation for the Kiro Cloud configuration editor."""
    if not isinstance(value, list) or len(value) > MAX_PROFILES:
        raise SessionEnvironmentsError(
            f"kiro_cloud_profiles must be a list of at most {MAX_PROFILES} entries"
        )
    result = [validate_kiro_cloud_profile(item, index) for index, item in enumerate(value)]
    if len({item["id"] for item in result}) != len(result):
        raise SessionEnvironmentsError("kiro_cloud_profiles contains duplicate profile IDs")
    return result


def load_kiro_cloud_profiles(raw: object) -> list[dict[str, Any]]:
    """Keep valid saved entries individually without weakening write validation."""
    if not isinstance(raw, dict):
        return []
    items = raw.get("kiro_cloud_profiles", [])
    if not isinstance(items, list):
        logger.warning("session_environments: kiro_cloud_profiles must be a list; ignoring it")
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        if len(result) >= MAX_PROFILES:
            break
        try:
            profile = validate_kiro_cloud_profile(item, index)
        except SessionEnvironmentsError:
            logger.warning("session_environments: dropping invalid Kiro Cloud profile %d", index)
            continue
        if profile["id"] not in seen:
            result.append(profile)
            seen.add(profile["id"])
    return result
