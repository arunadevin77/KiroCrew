#!/usr/bin/env python3
"""Publish a KAS engine release the way ``kirocrew kas install`` consumes it.

Takes the engine tarballs a KAS build produced (``kas-<platform>.tar.zst`` for
each of ``linux-x64``, ``linux-arm64``, ``darwin-universal``, ``win32-x64``),
adds one official Node build per host architecture from nodejs.org -- verified
against that release's ``SHASUMS256.txt`` -- and writes the manifest
:mod:`kiro_crew.acp.kas_bundle` reads::

    <out>/kas/<version>/kas-*.tar.zst        (copied)
    <out>/kas/<version>/node-v<N>-*.{tar.xz,zip}
    <out>/kas/<version>/manifest.json
    <out>/kas/latest.json

With ``--s3 s3://<bucket>`` the same tree is uploaded with the ``aws`` CLI,
``latest.json`` last so a reader never sees a pointer to a half-uploaded
release. The script never reads a credential itself; the CLI resolves its own.

Why a Node per architecture when the engine's Mac build is universal: Node
ships no universal binary, so ``node.builds`` is keyed by ``<os>-<arch>`` and
the installer picks the one for its host. The engine's own ``platforms`` map
keeps the engine's build matrix untouched.

Example::

    python3 scripts/kas_bundle_publish.py \\
        --kas-dir ~/kas-build/0.66.33 --version 0.66.33 \\
        --node-version 22.23.3 --out ./publish \\
        --s3 s3://kirocrew-kas
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

NODE_DIST = "https://nodejs.org/dist"
#: ``kirocrew`` host key -> nodejs.org archive name suffix.
NODE_BUILDS = {
    "linux-x64": "linux-x64.tar.xz",
    "linux-arm64": "linux-arm64.tar.xz",
    "darwin-x64": "darwin-x64.tar.xz",
    "darwin-arm64": "darwin-arm64.tar.xz",
    "win32-x64": "win-x64.zip",
}
ENGINE_PLATFORMS = ("linux-x64", "linux-arm64", "darwin-universal", "win32-x64")
#: The engine's declared floor (``@kiro/agent`` ``engines.node``); refuse older.
NODE_MIN = (22, 13, 0)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ref(path: Path) -> dict:
    return {"file": path.name, "sha256": sha256_of(path), "size": path.stat().st_size}


def fetch(url: str, dest: Path) -> None:
    if dest.exists():
        return
    print(f"  GET {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=600) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out)
    tmp.replace(dest)


def node_builds(version: str, out_dir: Path) -> dict[str, dict]:
    parts = tuple(int(x) for x in version.split("."))
    if parts < NODE_MIN:
        sys.exit(f"Node {version} is below the engine's floor {'.'.join(map(str, NODE_MIN))}")
    base = f"{NODE_DIST}/v{version}"
    sums_path = out_dir / f"SHASUMS256-v{version}.txt"
    fetch(f"{base}/SHASUMS256.txt", sums_path)
    sums = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        sums[name.strip()] = digest.strip()
    builds: dict[str, dict] = {}
    for key, suffix in NODE_BUILDS.items():
        name = f"node-v{version}-{suffix}"
        dest = out_dir / name
        fetch(f"{base}/{name}", dest)
        actual = sha256_of(dest)
        if sums.get(name) != actual:
            sys.exit(f"{name}: sha256 {actual} does not match nodejs.org SHASUMS256.txt")
        builds[key] = {"file": name, "sha256": actual, "size": dest.stat().st_size}
        print(f"  ✓ {name}")
    sums_path.unlink()
    return builds


def engine_platforms(kas_dir: Path, out_dir: Path) -> dict[str, dict]:
    platforms: dict[str, dict] = {}
    upstream = kas_dir / "manifest.json"
    declared = {}
    if upstream.is_file():
        declared = json.loads(upstream.read_text(encoding="utf-8")).get("platforms") or {}
    for key in ENGINE_PLATFORMS:
        name = (declared.get(key) or {}).get("file") or f"kas-{key}.tar.zst"
        src = kas_dir / name
        if not src.is_file():
            print(f"  ! no engine tarball for {key} ({src.name}); skipping that platform")
            continue
        dest = out_dir / name
        if not dest.exists():
            shutil.copyfile(src, dest)
        platforms[key] = ref(dest)
        if declared.get(key) and declared[key].get("sha256") != platforms[key]["sha256"]:
            sys.exit(f"{name}: sha256 differs from the upstream manifest.json beside it")
        print(f"  ✓ {name}")
    if not platforms:
        sys.exit(f"no kas-*.tar.zst found in {kas_dir}")
    return platforms


def upload(out_root: Path, version: str, s3_base: str, *, dry_run: bool) -> None:
    s3_base = s3_base.rstrip("/")
    release = out_root / "kas" / version
    files = sorted(p for p in release.iterdir() if p.is_file())
    cmds = [["aws", "s3", "cp", str(p), f"{s3_base}/kas/{version}/{p.name}"] for p in files]
    cmds.append(
        ["aws", "s3", "cp", str(out_root / "kas" / "latest.json"), f"{s3_base}/kas/latest.json"]
    )
    for cmd in cmds:
        print("  $", " ".join(cmd))
        if not dry_run:
            subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--kas-dir", required=True, type=Path, help="directory holding kas-*.tar.zst")
    ap.add_argument("--version", required=True, help="engine version, e.g. 0.66.33")
    ap.add_argument("--node-version", required=True, help="Node version to bundle, e.g. 22.23.3")
    ap.add_argument("--out", required=True, type=Path, help="output root (gets kas/<version>/)")
    ap.add_argument("--package", default="@kiro/agent")
    ap.add_argument("--s3", default=None, help="s3://bucket[/prefix] to upload to with the aws CLI")
    ap.add_argument("--dry-run", action="store_true", help="print the upload commands only")
    args = ap.parse_args()

    release = args.out / "kas" / args.version
    release.mkdir(parents=True, exist_ok=True)

    print(f"Engine tarballs ({args.version}):")
    platforms = engine_platforms(args.kas_dir, release)
    print(f"Node runtime ({args.node_version}):")
    builds = node_builds(args.node_version, release)

    manifest = {
        "version": args.version,
        "package": args.package,
        "platforms": platforms,
        "node": {"version": args.node_version, "builds": builds},
    }
    (release / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (args.out / "kas" / "latest.json").write_text(
        json.dumps({"version": args.version, "manifest": f"kas/{args.version}/manifest.json"})
        + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {release / 'manifest.json'} and {args.out / 'kas' / 'latest.json'}")
    print("Pin it in src/kiro_crew/acp/kas_pin.py (after testing):")
    print(f'  KAS_PIN_VERSION = "{args.version}"')
    print(f'  KAS_PIN_MANIFEST_SHA256 = "{sha256_of(release / "manifest.json")}"')
    if args.s3:
        print("Upload:")
        upload(args.out, args.version, args.s3, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
