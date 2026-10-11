"""The committed agent settings registry keeps LF bytes on every checkout.

``website/scripts/lib/ui-index.mjs`` (``checkSettingsExtraction``) compares the
committed ``src/kiro_crew/docs/settings-registry.generated.json`` with the live
extraction byte for byte, and the extraction writes LF. Under
``core.autocrlf=true`` (the Git-for-Windows default, and the Windows runners')
an unpinned JSON file is checked out with CRLF, so every Windows build that runs
``gen-ui-index --check`` fails with "settings-registry.generated.json does not
match the live settings extraction" while Linux and macOS pass.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REGISTRY = "src/kiro_crew/docs/settings-registry.generated.json"


@pytest.mark.parametrize("autocrlf", ["true", "input", "false"])
def test_checkout_preserves_the_registry_bytes(tmp_path: Path, autocrlf: str) -> None:
    committed = (_REPO_ROOT / _REGISTRY).read_bytes()
    assert b"\r\n" not in committed, "the committed registry itself must be LF"
    repo = tmp_path / "repo"
    (repo / Path(_REGISTRY).parent).mkdir(parents=True)
    (repo / _REGISTRY).write_bytes(committed)
    (repo / ".gitattributes").write_bytes((_REPO_ROOT / ".gitattributes").read_bytes())
    (repo / "newline-control.txt").write_bytes(b"control\n")
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    # Real Git conversion, without inheriting host or repository overrides.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    git = ["git", "-c", f"core.autocrlf={autocrlf}", "-c", f"core.attributesFile={os.devnull}"]
    for args in (
        ["init", "--quiet"],
        ["add", "--", ".gitattributes", _REGISTRY, "newline-control.txt"],
        [
            "checkout-index",
            f"--prefix={checkout.as_posix()}/",
            "--",
            _REGISTRY,
            "newline-control.txt",
        ],
    ):
        subprocess.run(git + args, cwd=repo, env=env, capture_output=True, check=True, timeout=30)

    # The control proves autocrlf=true really performs a CRLF checkout here.
    expected_control = b"control\r\n" if autocrlf == "true" else b"control\n"
    assert (checkout / "newline-control.txt").read_bytes() == expected_control
    assert (checkout / _REGISTRY).read_bytes() == committed
