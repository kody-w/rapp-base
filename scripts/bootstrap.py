#!/usr/bin/env python3
"""Safely bind an exported zero-state tree to a GitHub deployment."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rapp_base.build import build
from rapp_base.constants import API_PREFIX
from rapp_base.errors import RappError
from rapp_base.jsonutil import (
    expect_keys,
    load_json_file,
    require_hash,
    write_bytes_atomic,
    write_json_atomic,
)
from rapp_base.manifest import load_manifest
from rapp_base.state import head_for_events

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_REPOSITORY_CONTINUATION = r"A-Za-z0-9_.-"
_SEARCHABLE_SUFFIXES = frozenset(
    {
        ".cfg",
        ".css",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".md",
        ".mjs",
        ".py",
        ".sh",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_SEARCHABLE_NAMES = frozenset({"LICENSE", "Makefile", ".gitignore"})
_TOP_LEVEL_ALLOWLIST = frozenset(
    {
        ".gitignore",
        "CONTRIBUTING.md",
        "Makefile",
        "README.md",
        "SECURITY.md",
        "SPEC.md",
        "explorer.js",
        "index.html",
        "llms.txt",
        "styles.css",
    }
)
_EXACT_REWRITE_PATHS = _TOP_LEVEL_ALLOWLIST | frozenset(
    {
        ".github/ISSUE_TEMPLATE/config.yml",
        "tests/fixtures/issues.json",
        "tests/helpers.py",
        "tests/test_delivery.py",
        "tests/test_github_adapter.py",
    }
)
_DIRECTORY_ALLOWLIST = {
    ".well-known": frozenset({".json"}),
    "schemas": frozenset({".json"}),
}
_HEAD_LIMITS = {
    "array_items": 8,
    "json_depth": 4,
    "json_nodes": 32,
    "object_keys": 16,
    "string_bytes": 4096,
}


def _validate_target(owner: str, repository: str) -> None:
    if _OWNER_RE.fullmatch(owner) is None or "--" in owner:
        raise RappError(
            "invalid_owner",
            "owner must be a 1-39 character GitHub name using letters, "
            "digits, or single interior hyphens",
        )
    if (
        _REPOSITORY_RE.fullmatch(repository) is None
        or repository in {".", ".."}
    ):
        raise RappError(
            "invalid_repository",
            "repo must be a 1-100 character GitHub-safe path segment",
        )


def _validate_root(value: Path) -> Path:
    absolute = Path(os.path.abspath(value))
    if absolute.is_symlink():
        raise RappError("symlink", "bootstrap root cannot be a symlink")
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise RappError("invalid_root", "bootstrap root does not exist") from exc
    if not resolved.is_dir():
        raise RappError("invalid_root", "bootstrap root must be a directory")
    return resolved


def _reject_symlinks(root: Path) -> None:
    def reject_walk_error(error: OSError) -> None:
        raise RappError("walk_failed", f"cannot inspect repository: {error.filename}")

    for current, directories, files in os.walk(
        root,
        topdown=True,
        onerror=reject_walk_error,
        followlinks=False,
    ):
        current_path = Path(current)
        for name in list(directories):
            path = current_path / name
            if path.is_symlink():
                raise RappError(
                    "symlink",
                    f"repository symlinks are forbidden: {path.relative_to(root)}",
                )
            if path.name.endswith((".new", ".stage")):
                raise RappError(
                    "staging_file",
                    f"staging path is forbidden: {path.relative_to(root)}",
                )
            if name == ".git":
                if current_path != root:
                    raise RappError(
                        "nested_git",
                        f"nested .git content is forbidden: {path.relative_to(root)}",
                    )
                directories.remove(name)
        for name in files:
            path = current_path / name
            if path.is_symlink():
                raise RappError(
                    "symlink",
                    f"repository symlinks are forbidden: {path.relative_to(root)}",
                )
            if path.name.endswith((".new", ".stage")):
                raise RappError(
                    "staging_file",
                    f"staging file is forbidden: {path.relative_to(root)}",
                )
            if name == ".git" and current_path != root:
                raise RappError(
                    "nested_git",
                    f"nested .git content is forbidden: {path.relative_to(root)}",
                )


def _git_metadata_exists(root: Path) -> bool:
    return os.path.lexists(root / ".git")


def _run_git(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=False,
            capture_output=True,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except OSError as exc:
        raise RappError(
            "git_check",
            "git is required to validate a checkout before bootstrap",
        ) from exc


def _require_clean_checkout(root: Path) -> None:
    top = _run_git(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        if _git_metadata_exists(root):
            raise RappError("git_check", "root contains invalid .git metadata")
        return
    try:
        top_level = Path(os.fsdecode(top.stdout).strip()).resolve(strict=True)
    except OSError as exc:
        raise RappError("git_check", "cannot resolve the Git worktree root") from exc
    if top_level != root:
        raise RappError(
            "git_check",
            "bootstrap root cannot be nested inside another Git worktree",
        )
    status = _run_git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if status.returncode != 0:
        raise RappError("git_check", "cannot inspect Git worktree state")
    if status.stdout:
        raise RappError(
            "dirty_worktree",
            "Git worktree has staged, modified, or untracked files",
        )


def _validate_zero_state(root: Path) -> None:
    for name in ("requests", "receipts", "events"):
        directory = root / "state" / name
        if not directory.is_dir():
            raise RappError("missing_state", f"state/{name} must be a directory")
        json_files = [
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.lower() == ".json"
        ]
        if json_files:
            raise RappError(
                "admitted_state",
                f"state/{name} contains {len(json_files)} JSON file(s)",
            )

    head_path = root / "state" / "head.json"
    head = load_json_file(head_path, _HEAD_LIMITS, byte_limit=4096)
    expect_keys(
        head,
        required={
            "schema",
            "sequence",
            "event_hash",
            "event_path",
            "genesis_sha256",
        },
        context="bootstrap head",
    )
    if head["schema"] != "rapp-base-head/1.0":
        raise RappError("invalid_head", "state head schema is invalid")
    sequence = head["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence != 0:
        raise RappError("admitted_state", "state head sequence must be exactly 0")
    if head["event_hash"] is not None or head["event_path"] is not None:
        raise RappError("invalid_head", "zero-state head cannot reference an event")
    require_hash(head["genesis_sha256"], "head genesis_sha256")


def _deployment_forms(
    old_owner: str,
    old_repository: str,
    new_owner: str,
    new_repository: str,
) -> tuple[tuple[re.Pattern[str], str], ...]:
    old_full_name = f"{old_owner}/{old_repository}"
    new_full_name = f"{new_owner}/{new_repository}"
    values = (
        (
            f"https://github.com/{old_full_name}.git",
            f"https://github.com/{new_full_name}.git",
        ),
        (
            f"git@github.com:{old_full_name}.git",
            f"git@github.com:{new_full_name}.git",
        ),
        (
            f"https://{old_owner}.github.io/{old_repository}/schemas/",
            f"https://{new_owner}.github.io/{new_repository}/schemas/",
        ),
        (
            f"https://raw.githubusercontent.com/{old_full_name}/main",
            f"https://raw.githubusercontent.com/{new_full_name}/main",
        ),
        (
            f"https://{old_owner}.github.io/{old_repository}",
            f"https://{new_owner}.github.io/{new_repository}",
        ),
        (
            f"https://github.com/{old_full_name}",
            f"https://github.com/{new_full_name}",
        ),
        (old_full_name, new_full_name),
    )
    return tuple(
        (
            re.compile(
                rf"(?<![{_REPOSITORY_CONTINUATION}])"
                rf"{re.escape(old)}"
                rf"(?![{_REPOSITORY_CONTINUATION}])"
            ),
            new,
        )
        for old, new in values
    )


def _replace_deployment_forms(
    text: str,
    forms: tuple[tuple[re.Pattern[str], str], ...],
) -> str:
    for pattern, replacement in forms:
        text = pattern.sub(lambda _match: replacement, text)
    return text


def _contains_deployment_form(
    text: str,
    forms: tuple[tuple[re.Pattern[str], str], ...],
) -> bool:
    return any(pattern.search(text) is not None for pattern, _replacement in forms)


def _tracked_or_exported_files(root: Path) -> list[Path]:
    if _git_metadata_exists(root):
        result = _run_git(root, "ls-files", "-z")
        if result.returncode != 0:
            raise RappError("git_check", "cannot enumerate tracked files")
        paths = []
        for value in result.stdout.split(b"\0"):
            if not value:
                continue
            relative = Path(os.fsdecode(value))
            if relative.is_absolute() or ".." in relative.parts:
                raise RappError("git_check", "Git returned an unsafe tracked path")
            path = root / relative
            if path.is_file():
                paths.append(path)
        return sorted(paths)
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )


def _verification_files(root: Path) -> list[Path]:
    paths = set(_tracked_or_exported_files(root))
    if _git_metadata_exists(root):
        registry = root / "registry.json"
        if registry.is_file():
            paths.add(registry)
        for name in ("api", "versions"):
            directory = root / name
            if directory.is_dir():
                paths.update(path for path in directory.rglob("*") if path.is_file())
    return sorted(paths)


def _is_searchable(path: Path) -> bool:
    return path.name in _SEARCHABLE_NAMES or path.suffix.lower() in _SEARCHABLE_SUFFIXES


def _is_rewrite_allowlisted(relative: Path) -> bool:
    text = relative.as_posix()
    if text in _EXACT_REWRITE_PATHS:
        return True
    if not relative.parts:
        return False
    suffixes = _DIRECTORY_ALLOWLIST.get(relative.parts[0])
    return suffixes is not None and relative.suffix.lower() in suffixes


def _is_generated(relative: Path) -> bool:
    return (
        relative.as_posix() == "registry.json"
        or (
            bool(relative.parts)
            and relative.parts[0] in {"api", "versions"}
        )
    )


def _updated_manifest(
    manifest: dict[str, Any],
    owner: str,
    repository: str,
) -> dict[str, Any]:
    old_owner = manifest["repository"]["owner"]
    old_repository = manifest["repository"]["name"]
    old_github = f"https://github.com/{old_owner}/{old_repository}"
    new_github = f"https://github.com/{owner}/{repository}"
    old_pages = f"https://{old_owner}.github.io/{old_repository}/"
    new_pages = f"https://{owner}.github.io/{repository}/"
    homepage_replacements = {
        old_github: new_github,
        f"{old_github}/": f"{new_github}/",
        old_pages: new_pages,
        old_pages.removesuffix("/"): new_pages.removesuffix("/"),
    }

    updated = copy.deepcopy(manifest)
    updated["repository"] = {
        "owner": owner,
        "name": repository,
        "branch": "main",
    }
    for collection in updated["collections"]:
        for seed in collection["seed"]:
            data = seed["data"]
            homepage = data.get("homepage")
            if homepage in homepage_replacements:
                data["homepage"] = homepage_replacements[homepage]
    return updated


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _prepare_rewrites(
    root: Path,
    manifest: dict[str, Any],
    owner: str,
    repository: str,
) -> tuple[dict[Path, bytes], dict[str, Any], tuple[tuple[re.Pattern[str], str], ...]]:
    old_owner = manifest["repository"]["owner"]
    old_repository = manifest["repository"]["name"]
    forms = _deployment_forms(old_owner, old_repository, owner, repository)
    updated_manifest = _updated_manifest(manifest, owner, repository)
    manifest_data = _manifest_bytes(updated_manifest)
    manifest_text = manifest_data.decode("utf-8")
    if _contains_deployment_form(manifest_text, forms):
        raise RappError(
            "manifest_reference",
            "manifest contains a deployment reference outside repository identity "
            "or an exact deployment-local seed homepage",
        )

    rewrites: dict[Path, bytes] = {root / "manifest.json": manifest_data}
    for path in _tracked_or_exported_files(root):
        relative = path.relative_to(root)
        if relative.as_posix() == "manifest.json" or _is_generated(relative):
            continue
        if not _is_searchable(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise RappError(
                "invalid_text",
                f"searchable file is not UTF-8: {relative}",
            ) from exc
        if not _contains_deployment_form(text, forms):
            continue
        if not _is_rewrite_allowlisted(relative):
            raise RappError(
                "rewrite_allowlist",
                f"deployment reference is outside the rewrite allowlist: {relative}",
            )
        updated = _replace_deployment_forms(text, forms)
        if _contains_deployment_form(updated, forms):
            raise RappError(
                "stale_reference",
                f"deployment reference could not be rewritten: {relative}",
            )
        rewrites[path] = updated.encode("utf-8")
    return rewrites, updated_manifest, forms


def _validate_generated_paths(root: Path) -> None:
    for name in ("api", "versions"):
        path = root / name
        if path.exists() and not path.is_dir():
            raise RappError("generated_path", f"{name} must be a directory")
    registry = root / "registry.json"
    if registry.exists() and not registry.is_file():
        raise RappError("generated_path", "registry.json must be a file")


def _remove_generated(root: Path) -> None:
    api = root / API_PREFIX
    if api.exists():
        shutil.rmtree(api)
    versions = root / "versions"
    if versions.exists():
        shutil.rmtree(versions)
    registry = root / "registry.json"
    if registry.exists():
        registry.unlink()


def _verify_no_stale_references(
    root: Path,
    forms: tuple[tuple[re.Pattern[str], str], ...],
) -> None:
    stale = []
    for path in _verification_files(root):
        if not _is_searchable(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeError:
            continue
        if _contains_deployment_form(text, forms):
            stale.append(path.relative_to(root).as_posix())
    if stale:
        raise RappError(
            "stale_reference",
            f"old deployment reference remains in: {', '.join(stale)}",
        )


def bootstrap(root: Path, owner: str, repository: str) -> dict[str, Any]:
    _validate_target(owner, repository)
    root = _validate_root(root)
    _reject_symlinks(root)
    _require_clean_checkout(root)
    _validate_zero_state(root)
    manifest = load_manifest(root)
    old_full_name = (
        f"{manifest['repository']['owner']}/{manifest['repository']['name']}"
    )
    new_full_name = f"{owner}/{repository}"
    if old_full_name == new_full_name:
        raise RappError(
            "same_repository",
            "target owner/repo already matches the manifest",
        )

    rewrites, updated_manifest, forms = _prepare_rewrites(
        root,
        manifest,
        owner,
        repository,
    )
    _validate_generated_paths(root)
    for path, data in sorted(
        rewrites.items(),
        key=lambda item: item[0].relative_to(root).as_posix(),
    ):
        write_bytes_atomic(path, data)

    _remove_generated(root)
    write_json_atomic(
        root / "state" / "head.json",
        head_for_events(updated_manifest, []),
    )
    manifest = load_manifest(root)
    summary = build(root, manifest, write=True)
    _verify_no_stale_references(root, forms)
    return {
        "from": old_full_name,
        "generated": summary,
        "rewritten_files": len(rewrites),
        "to": new_full_name,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="bind a clean zero-state export to a GitHub deployment"
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()
    try:
        summary = bootstrap(args.root, args.owner, args.repo)
    except (OSError, RappError) as exc:
        if isinstance(exc, RappError):
            message = f"[{exc.code}] {exc.message}"
        else:
            message = str(exc)
        print(f"bootstrap refused: {message}", file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
