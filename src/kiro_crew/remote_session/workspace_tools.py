"""Guest-side MCP tool server for a remote session workspace.

The agent's engine runs on the gateway; this server is the only way it touches
the remote workspace. It speaks MCP over newline-delimited JSON-RPC on stdio
and is started by ``worker.py`` inside the guest, so the gateway reaches it
through the authenticated, integrity-checked ``wire`` stream.

Stdlib-only and Python 3.9 compatible: guest images ship older interpreters.

Every file path is resolved (symlinks included) and must stay inside ``--root``;
a path that escapes is refused, not clamped. Results are capped so one large
file or noisy command cannot flood a slow tunnel. ``run_command`` is an
unrestricted shell in the root, by design: the workspace is disposable and every
call still goes through Kiro Crew's normal approval flow on the gateway.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional

SERVER_NAME = "kirocrew-workspace"
SERVER_VERSION = "1"
DEFAULT_PROTOCOL = "2024-11-05"
MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_LIST_ENTRIES = 1000
MAX_MATCHES = 200
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 1800
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__", ".mypy_cache"})
#: Never handed to a command the agent runs.
WITHHELD_ENV = frozenset(
    {
        "KIRO_API_KEY",
        "KIRO_IDENTITY",
        "CODER_AGENT_TOKEN",
        "CODER_AGENT_TOKEN_FILE",
        "CODER_SESSION_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)


#: Written by ``git_proxy.py`` once it listens; absent when no proxy runs.
GIT_PROXY_PORT_FILE = os.path.join(os.path.expanduser("~"), ".kirocrew-worker", "git-proxy.port")
#: Per proxied host, the remote spellings git rewrites to the proxy (each a
#: prefix git matches). Mirrors ``git_gateway.HOSTS``, which this guest-side
#: file cannot import; ``test_remote_git_proxy`` pins the two equal.
GIT_PROXY_REWRITES: Dict[str, tuple] = {
    "github.com": ("https://github.com/", "git@github.com:", "ssh://git@github.com/"),
    "gitlab.com": ("https://gitlab.com/", "git@gitlab.com:", "ssh://git@gitlab.com/"),
    "bitbucket.org": ("https://bitbucket.org/", "git@bitbucket.org:", "ssh://git@bitbucket.org/"),
    "dev.azure.com": ("https://dev.azure.com/",),
}


def _listening(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def git_proxy_env(port_file: str = GIT_PROXY_PORT_FILE) -> Dict[str, str]:
    """Environment that routes this command's GitHub git traffic via the gateway.

    Uses git's environment config (``GIT_CONFIG_COUNT``, git 2.31+), so nothing
    is written to a git config file and a command run outside this tool is not
    affected. ``GIT_TERMINAL_PROMPT=0`` and an empty ``GIT_ASKPASS`` keep git
    from asking anyone for a password: the proxy never challenges, so a prompt
    could only come from a remote the proxy does not serve.
    """
    try:
        with open(port_file, encoding="ascii") as handle:
            port = int(handle.read().strip())
    except (OSError, ValueError):
        return {}
    if not 0 < port < 65536 or not _listening(port):
        # A file left by a proxy that died: rewriting to a dead port would break
        # even public clones, so the command runs as if no proxy existed.
        return {}
    pairs = [
        (f"url.http://127.0.0.1:{port}/{host}/.insteadOf", prefix)
        for host, prefixes in GIT_PROXY_REWRITES.items()
        for prefix in prefixes
    ]
    env = {"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "GIT_CONFIG_COUNT": str(len(pairs))}
    for index, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


class ToolError(Exception):
    """A refusal or failure reported to the agent as a tool error, not a crash."""


class Workspace:
    def __init__(self, root: str) -> None:
        real = os.path.realpath(root)
        if not os.path.isdir(real):
            raise SystemExit(f"workspace root {root!r} is not a directory")
        self.root = real

    # -- paths -----------------------------------------------------------------
    def resolve(self, path: Any, *, must_exist: bool = True) -> str:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ToolError("path must be a non-empty string")
        joined = path if os.path.isabs(path) else os.path.join(self.root, path)
        real = os.path.realpath(joined)
        if real != self.root and not real.startswith(self.root + os.sep):
            raise ToolError(f"path {path!r} is outside the workspace {self.root}")
        if must_exist and not os.path.exists(real):
            raise ToolError(f"{path!r} does not exist")
        return real

    def rel(self, real: str) -> str:
        return os.path.relpath(real, self.root)

    # -- tools -----------------------------------------------------------------
    def read_file(self, path: str, offset: int = 0, limit: int = 0) -> str:
        real = self.resolve(path)
        if os.path.isdir(real):
            raise ToolError(f"{path!r} is a directory; use list_directory")
        with open(real, "rb") as handle:
            data = handle.read(MAX_READ_BYTES + 1)
        truncated = len(data) > MAX_READ_BYTES
        text = data[:MAX_READ_BYTES].decode("utf-8", "replace")
        if offset or limit:
            lines = text.splitlines(keepends=True)
            end = offset + limit if limit else len(lines)
            text = "".join(lines[max(offset, 0) : end])
        if truncated:
            text += f"\n[truncated at {MAX_READ_BYTES} bytes; use offset/limit]"
        return text

    def write_file(self, path: str, content: str) -> str:
        if not isinstance(content, str):
            raise ToolError("content must be a string")
        data = content.encode("utf-8")
        if len(data) > MAX_WRITE_BYTES:
            raise ToolError(f"content exceeds {MAX_WRITE_BYTES} bytes")
        real = self.resolve(path, must_exist=False)
        parent = os.path.dirname(real)
        # Re-check after creating parents: a symlinked parent could point out.
        os.makedirs(parent, exist_ok=True)
        self.resolve(parent)
        tmp = f"{real}.kirocrew-tmp"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, real)
        return f"wrote {len(data)} bytes to {self.rel(real)}"

    def edit_file(self, path: str, old_str: str, new_str: str, replace_all: bool = False) -> str:
        real = self.resolve(path)
        with open(real, "r", encoding="utf-8") as handle:
            text = handle.read()
        if not old_str:
            raise ToolError("old_str must not be empty")
        count = text.count(old_str)
        if count == 0:
            raise ToolError("old_str was not found")
        if count > 1 and not replace_all:
            raise ToolError(f"old_str occurs {count} times; pass replace_all or add context")
        updated = (
            text.replace(old_str, new_str) if replace_all else text.replace(old_str, new_str, 1)
        )
        self.write_file(path, updated)
        return f"replaced {count if replace_all else 1} occurrence(s) in {self.rel(real)}"

    def list_directory(self, path: str = ".") -> str:
        real = self.resolve(path)
        if not os.path.isdir(real):
            raise ToolError(f"{path!r} is not a directory")
        entries = sorted(os.listdir(real))
        lines = []
        for name in entries[:MAX_LIST_ENTRIES]:
            full = os.path.join(real, name)
            lines.append(name + ("/" if os.path.isdir(full) else ""))
        if len(entries) > MAX_LIST_ENTRIES:
            lines.append(f"[{len(entries) - MAX_LIST_ENTRIES} more entries not shown]")
        return "\n".join(lines) or "(empty)"

    def _walk(self, base: str):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                yield os.path.join(dirpath, name)

    def glob(self, pattern: str, path: str = ".") -> str:
        base = self.resolve(path)
        found = []
        for full in self._walk(base):
            rel = os.path.relpath(full, base)
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(os.path.basename(full), pattern):
                found.append(self.rel(full))
                if len(found) >= MAX_LIST_ENTRIES:
                    found.append("[more matches not shown]")
                    break
        return "\n".join(found) or "(no matches)"

    def grep(self, pattern: str, path: str = ".", include: str = "") -> str:
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"invalid regex: {exc}") from exc
        base = self.resolve(path)
        files = [base] if os.path.isfile(base) else self._walk(base)
        hits: List[str] = []
        for full in files:
            if include and not fnmatch.fnmatch(os.path.basename(full), include):
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="strict") as handle:
                    for number, line in enumerate(handle, 1):
                        if regex.search(line):
                            hits.append(f"{self.rel(full)}:{number}: {line.rstrip()[:300]}")
                            if len(hits) >= MAX_MATCHES:
                                return "\n".join(hits) + "\n[match limit reached]"
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable
        return "\n".join(hits) or "(no matches)"

    def run_command(self, command: str, timeout: int = DEFAULT_TIMEOUT, cwd: str = ".") -> str:
        if not isinstance(command, str) or not command.strip():
            raise ToolError("command must be a non-empty string")
        timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        workdir = self.resolve(cwd)
        env = {k: v for k, v in os.environ.items() if k not in WITHHELD_ENV}
        # GitHub git traffic goes through the gateway, which holds the credential.
        env.update(git_proxy_env())
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", command],
                cwd=workdir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                start_new_session=True,
            )
            output, code = proc.stdout, proc.returncode
        except subprocess.TimeoutExpired as exc:
            output, code = exc.stdout or b"", None
        text = output.decode("utf-8", "replace")
        if len(text) > MAX_OUTPUT_BYTES:
            # Keep the tail: errors and summaries come last.
            text = (
                f"[{len(text) - MAX_OUTPUT_BYTES} earlier bytes omitted]\n"
                + text[-MAX_OUTPUT_BYTES:]
            )
        status = f"timed out after {timeout}s" if code is None else f"exit {code}"
        return f"{text}\n[{status}]"


def _schema(properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


_S = {"type": "string"}
_I = {"type": "integer"}


def tool_specs(root: str) -> List[Dict[str, Any]]:
    where = f"in the remote workspace ({root}). Relative paths are relative to it."
    return [
        {
            "name": "read_file",
            "description": f"Read a text file {where}",
            "inputSchema": _schema({"path": _S, "offset": _I, "limit": _I}, ["path"]),
        },
        {
            "name": "write_file",
            "description": f"Create or overwrite a file {where}",
            "inputSchema": _schema({"path": _S, "content": _S}, ["path", "content"]),
        },
        {
            "name": "edit_file",
            "description": f"Replace an exact string in a file {where} old_str must be unique unless replace_all.",
            "inputSchema": _schema(
                {"path": _S, "old_str": _S, "new_str": _S, "replace_all": {"type": "boolean"}},
                ["path", "old_str", "new_str"],
            ),
        },
        {
            "name": "list_directory",
            "description": f"List a directory {where}",
            "inputSchema": _schema({"path": _S}, []),
        },
        {
            "name": "glob",
            "description": f"Find files by glob pattern {where}",
            "inputSchema": _schema({"pattern": _S, "path": _S}, ["pattern"]),
        },
        {
            "name": "grep",
            "description": f"Search file contents with a regex {where}",
            "inputSchema": _schema({"pattern": _S, "path": _S, "include": _S}, ["pattern"]),
        },
        {
            "name": "run_command",
            "description": f"Run a shell command {where} Returns combined output and exit status.",
            "inputSchema": _schema({"command": _S, "timeout": _I, "cwd": _S}, ["command"]),
        },
    ]


class Server:
    def __init__(self, workspace: Workspace) -> None:
        self.ws = workspace
        self.tools: Dict[str, Callable[..., str]] = {
            spec["name"]: getattr(workspace, spec["name"]) for spec in tool_specs(workspace.root)
        }

    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        method, ident = msg.get("method"), msg.get("id")
        if ident is None:
            return None  # notification
        try:
            result = self._dispatch(method, msg.get("params") or {})
            return {"jsonrpc": "2.0", "id": ident, "result": result}
        except _RpcError as exc:
            return {"jsonrpc": "2.0", "id": ident, "error": {"code": exc.code, "message": str(exc)}}

    def _dispatch(self, method: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": tool_specs(self.ws.root)}
        if method == "tools/call":
            name = params.get("name")
            fn = self.tools.get(name) if isinstance(name, str) else None
            if fn is None:
                raise _RpcError(-32602, f"unknown tool {name!r}")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise _RpcError(-32602, "arguments must be an object")
            try:
                text, is_error = fn(**arguments), False
            except ToolError as exc:
                text, is_error = str(exc), True
            except TypeError as exc:
                text, is_error = f"invalid arguments: {exc}", True
            except OSError as exc:
                text, is_error = f"{type(exc).__name__}: {exc}", True
            return {"content": [{"type": "text", "text": text}], "isError": is_error}
        raise _RpcError(-32601, f"method {method!r} not found")


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def serve(server: Server, inp=None, out=None) -> None:
    inp = inp or sys.stdin.buffer
    out = out or sys.stdout.buffer
    for line in inp:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        reply = server.handle(msg)
        if reply is not None:
            out.write(json.dumps(reply).encode("utf-8") + b"\n")
            out.flush()


def browse(ws: Workspace, request: Any) -> Dict[str, Any]:
    """One READ-ONLY request for the dashboard's file browser.

    Only ``list`` and ``read``: the browser is a view, so this entry point can
    never write, edit or run anything, whatever the request says. Same root
    confinement and caps as the agent's tools.
    """
    if not isinstance(request, dict):
        return {"error": "request must be an object"}
    op, path = request.get("op"), request.get("path") or "."
    try:
        if op == "list":
            real = ws.resolve(path)
            if not os.path.isdir(real):
                raise ToolError(f"{path!r} is not a directory")
            names = sorted(os.listdir(real))
            entries = [
                {"name": n, "dir": os.path.isdir(os.path.join(real, n))}
                for n in names[:MAX_LIST_ENTRIES]
            ]
            return {
                "root": ws.root,
                "path": ws.rel(real),
                "entries": entries,
                "truncated": len(names) > MAX_LIST_ENTRIES,
            }
        if op == "read":
            real = ws.resolve(path)
            if os.path.isdir(real):
                raise ToolError(f"{path!r} is a directory")
            with open(real, "rb") as handle:
                data = handle.read(MAX_READ_BYTES + 1)
            return {
                "root": ws.root,
                "path": ws.rel(real),
                "content": data[:MAX_READ_BYTES].decode("utf-8", "replace"),
                "binary": b"\x00" in data[:8192],
                "truncated": len(data) > MAX_READ_BYTES,
            }
        if op in ("git_status", "git_log", "git_diff"):
            return _git_read(ws, op, path)
        if op == "stats":
            return _stats(ws)
    except ToolError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"error": "op must be list, read, git_status, git_log or git_diff"}


def _meminfo() -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                fields = rest.split()
                if key in ("MemTotal", "MemAvailable") and fields and fields[0].isdigit():
                    out[key] = int(fields[0]) * 1024
    except OSError:
        pass
    return out


def _stats(ws: Workspace) -> Dict[str, Any]:
    """Read-only machine metrics for the dashboard's environment card.

    Only numbers and the platform: no environment, no process list, no paths
    beyond the workspace root the caller already knows.
    """
    import platform
    import shutil

    result: Dict[str, Any] = {
        "root": ws.root,
        "os": platform.system(),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "cpus": os.cpu_count() or 0,
    }
    try:
        result["load"] = [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):
        pass
    mem = _meminfo()
    if "MemTotal" in mem and "MemAvailable" in mem:
        result["memory"] = {"total_bytes": mem["MemTotal"], "available_bytes": mem["MemAvailable"]}
    try:
        disk = shutil.disk_usage(ws.root)
        result["disk"] = {"total_bytes": disk.total, "free_bytes": disk.free}
    except OSError:
        pass
    try:
        with open("/proc/uptime", "r") as handle:
            result["uptime_s"] = int(float(handle.read().split()[0]))
    except (OSError, ValueError, IndexError):
        pass
    return result


def _git_read(ws: Workspace, op: str, path: str) -> Dict[str, Any]:
    """Read-only git views of the workspace root: fixed argv, no shell.

    ``-c core.fsmonitor= -c core.pager=cat`` and no hooks: a repository's own
    config must not turn a status read into running a program from it.
    """
    base = ["git", "-c", "core.fsmonitor=", "-c", "core.pager=cat", "-C", ws.root]
    if op == "git_status":
        argv = base + ["status", "--porcelain=v1", "--branch", "--untracked-files=normal"]
    elif op == "git_log":
        argv = base + ["log", "--no-color", "-n", "50", "--pretty=format:%h%x09%an%x09%ar%x09%s"]
    else:
        target = ws.resolve(path, must_exist=False) if path not in ("", ".") else ws.root
        argv = base + ["diff", "--no-color", "--no-ext-diff", "HEAD", "--", target]
    env = {k: v for k, v in os.environ.items() if k not in WITHHELD_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except FileNotFoundError:
        return {"root": ws.root, "git": False, "error_text": "git is not installed"}
    except subprocess.TimeoutExpired:
        raise ToolError("git did not answer in 30 seconds")
    out = proc.stdout[:MAX_READ_BYTES].decode("utf-8", "replace")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        # Not a repository (or no commits yet) is a state, not a failure.
        return {"root": ws.root, "git": False, "error_text": err[-300:]}
    return {
        "root": ws.root,
        "git": True,
        "output": out,
        "truncated": len(proc.stdout) > MAX_READ_BYTES,
    }


def write_request(ws: Workspace, request: Any) -> Dict[str, Any]:
    """The browser's ONE mutation: save a file's text, under the same root rule.

    A separate entry point from :func:`browse` so the read path can never write,
    and refusing a write whose ``expected`` text no longer matches the file, so
    saving a stale buffer cannot silently overwrite the agent's newer edit.
    """
    if not isinstance(request, dict):
        return {"error": "request must be an object"}
    path, content = request.get("path"), request.get("content")
    if not isinstance(content, str):
        return {"error": "content must be a string"}
    try:
        expected = request.get("expected")
        if isinstance(expected, str):
            real = ws.resolve(path, must_exist=False)
            if os.path.exists(real):
                with open(real, "rb") as handle:
                    current = handle.read(MAX_WRITE_BYTES + 1).decode("utf-8", "replace")
                if current != expected:
                    return {"error": "the file changed since it was opened", "conflict": True}
        if not isinstance(path, str):
            raise ToolError("path must be a non-empty string")
        message = ws.write_file(path, content)
        return {"root": ws.root, "path": ws.rel(ws.resolve(path)), "ok": True, "message": message}
    except ToolError as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="workspace_tools.py")
    p.add_argument("--root", required=True)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--browse",
        action="store_true",
        help="answer one read-only JSON request from stdin and exit",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="save one file from a JSON request on stdin and exit",
    )
    args = p.parse_args(argv)
    os.makedirs(os.path.expanduser(args.root), exist_ok=True)
    ws = Workspace(os.path.expanduser(args.root))
    if args.browse or args.write:
        limit = (2 * MAX_WRITE_BYTES) if args.write else 65536
        try:
            request = json.loads(sys.stdin.buffer.read(limit) or b"null")
        except ValueError:
            request = None
        result = write_request(ws, request) if args.write else browse(ws, request)
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
        return 0
    serve(Server(ws))
    return 0


if __name__ == "__main__":
    sys.exit(main())
