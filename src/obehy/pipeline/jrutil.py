"""The jrutil-rs runtime: how to build and invoke the compiler, and its provenance."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import file_digest


def git_identity(repository: Path) -> dict[str, Any]:
    safe = f"safe.directory={repository.resolve().as_posix()}"
    commit = subprocess.run(
        ["git", "-c", safe, "-C", str(repository), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-c", safe, "-C", str(repository), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    identity: dict[str, Any] = {
        "commit": commit,
        "dirty": bool(status),
        "status": status.splitlines(),
    }
    if status:
        diff = subprocess.run(
            ["git", "-c", safe, "-C", str(repository), "diff", "--binary", "HEAD"],
            capture_output=True,
            check=True,
        ).stdout
        identity["working_tree_sha256"] = hashlib.sha256(diff).hexdigest()
    return identity


def binary(root: Path) -> Path:
    """The release `jrutil` binary of a jrutil-rs checkout."""

    name = "jrutil.exe" if sys.platform == "win32" else "jrutil"
    return root / "target" / "release" / name


def build_command(root: Path) -> list[str]:
    """Build the release compiler of a jrutil-rs checkout (run in the checkout)."""

    if not (root / "Cargo.toml").is_file():
        raise PipelineError(f"jrutil-rs checkout has no Cargo.toml: {root}")
    return ["cargo", "build", "--release", "--locked", "-p", "jrutil"]


def runtime_command(root: Path | None, command: Sequence[str] | None) -> list[str]:
    """The compiler invocation: a configured command or the checkout's release binary."""

    if command is not None:
        return list(command)
    if root is None:
        raise PipelineError("jrutil runtime is not configured")
    return [str(binary(root))]


def provenance(root: Path | None, command: Sequence[str] | None) -> dict[str, Any]:
    """Identify the compiler: a git checkout or the files of a configured command."""

    if root is not None:
        return {"mode": "directory", "directory": str(root.resolve()), "git": git_identity(root)}
    if command is None:
        raise PipelineError("jrutil runtime is not configured")
    files: list[dict[str, object]] = []
    for argument in command:
        candidate = Path(argument)
        if candidate.is_absolute() and candidate.is_file():
            files.append(
                {
                    "path": str(candidate.resolve()),
                    "bytes": candidate.stat().st_size,
                    "sha256": file_digest(candidate),
                }
            )
    return {"mode": "command", "command": list(command), "files": files}
