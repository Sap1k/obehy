"""JrUtil runtime: how to build and invoke the multitool, and its provenance."""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

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


def multitool_project(root: Path) -> Path:
    return root / "jrutil-multitool" / "jrutil-multitool.fsproj"


def multitool_dll(root: Path) -> Path:
    return root / "jrutil-multitool" / "bin" / "Release" / "net10.0" / "jrutil-multitool.dll"


def build_command(root: Path) -> list[str]:
    """Build the Release multitool of a JrUtil checkout."""

    project = multitool_project(root)
    if not project.is_file():
        raise PipelineError(f"JrUtil multitool project does not exist: {project}")
    return ["dotnet", "build", str(project), "-c", "Release", "--no-restore", "--nologo"]


def runtime_command(root: Path | None, command: Sequence[str] | None) -> list[str]:
    """The multitool invocation: a configured command or the checkout's built DLL."""

    if command is not None:
        return list(command)
    if root is None:
        raise PipelineError("JrUtil runtime is not configured")
    return ["dotnet", str(multitool_dll(root))]


def provenance(root: Path | None, command: Sequence[str] | None) -> dict[str, Any]:
    """Identify the JrUtil runtime: a git checkout or the files of a configured command."""

    if root is not None:
        return {"mode": "directory", "directory": str(root.resolve()), "git": git_identity(root)}
    if command is None:
        raise PipelineError("JrUtil runtime is not configured")
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


def converter_version(runtime: Mapping[str, Any]) -> str:
    """The `--converter-version` recorded as a package's compiler version."""

    if runtime.get("mode") == "command":
        files = cast(list[dict[str, object]], runtime.get("files", []))
        if files:
            return f"command.{str(files[0]['sha256'])[:12]}"
        return "configured-command"
    git = cast(Mapping[str, Any], runtime["git"])
    commit = cast(str, git["commit"])
    dirty_hash = git.get("working_tree_sha256")
    return commit if dirty_hash is None else f"{commit}+dirty.{cast(str, dirty_hash)[:12]}"
