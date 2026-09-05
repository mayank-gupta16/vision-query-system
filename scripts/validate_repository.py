#!/usr/bin/env python3
"""Run zero-dependency repository policy checks for the bootstrap phase."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_FILES = (
    ".env.example",
    ".github/CODEOWNERS",
    ".github/ISSUE_TEMPLATE/bug.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/feature.yml",
    ".github/ISSUE_TEMPLATE/performance.yml",
    ".github/ISSUE_TEMPLATE/research.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/dependabot.yml",
    ".github/labels.yml",
    ".github/workflows/ci.yml",
    "AGENTS.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "README.md",
    "SECURITY.md",
    "THIRD_PARTY_NOTICES.md",
    "docs/index.md",
    "docs/project/current.md",
    "docs/project/roadmap.md",
)
PROHIBITED_TRACKED_SUFFIXES = {
    ".7z",
    ".avi",
    ".bmp",
    ".bin",
    ".ckpt",
    ".db",
    ".flac",
    ".gif",
    ".gz",
    ".jpeg",
    ".jpg",
    ".mkv",
    ".mov",
    ".mp4",
    ".mp3",
    ".npy",
    ".npz",
    ".onnx",
    ".parquet",
    ".pem",
    ".png",
    ".pt",
    ".pth",
    ".safetensors",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tiff",
    ".wav",
    ".webm",
    ".webp",
    ".zip",
}
SECRET_PATTERNS = {
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "OpenAI-style token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "generic assigned secret": re.compile(
        r"(?i)\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*[\"']?[A-Za-z0-9_+/%{}.-]{16,}"
    ),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
ACTION_USE = re.compile(r"^\s*uses:\s*([^\s#]+)", re.MULTILINE)
FULL_SHA_ACTION = re.compile(r"^[^/@\s]+/[^@\s]+@[0-9a-f]{40}$")


def candidate_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line]


def strip_fenced_code(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.DOTALL)


def check_markdown_links(path: Path, failures: list[str]) -> None:
    text = strip_fenced_code(path.read_text(encoding="utf-8"))
    for raw_target in MARKDOWN_LINK.findall(text):
        target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        relative = target.split("#", 1)[0]
        if not relative:
            continue
        resolved = (path.parent / relative).resolve()
        try:
            resolved.relative_to(ROOT)
        except ValueError:
            failures.append(f"{path.relative_to(ROOT)}: link escapes repository: {target}")
            continue
        if not resolved.exists():
            failures.append(f"{path.relative_to(ROOT)}: missing link target: {target}")


def main() -> int:
    failures: list[str] = []

    # Guard the guardrails against accidental weakening.
    assert FULL_SHA_ACTION.fullmatch("actions/checkout@" + "a" * 40)
    assert not FULL_SHA_ACTION.fullmatch("actions/checkout@v6")
    assert SECRET_PATTERNS["GitHub token"].search("ghp_" + "a" * 24)

    for relative in REQUIRED_FILES:
        path = ROOT / relative
        if not path.is_file() or path.stat().st_size == 0:
            failures.append(f"required file missing or empty: {relative}")

    for directory in sorted(path for path in (ROOT / "docs").rglob("*") if path.is_dir()):
        if not (directory / "index.md").is_file():
            failures.append(f"documentation directory lacks index.md: {directory.relative_to(ROOT)}")

    files = candidate_files()
    for path in files:
        relative = path.relative_to(ROOT)
        if path.is_symlink():
            failures.append(f"tracked/candidate symlink requires explicit review: {relative}")
            continue
        if not path.is_file():
            continue
        name = relative.as_posix()
        if (path.name == ".env" or path.name.startswith(".env.")) and name != ".env.example":
            failures.append(f"environment file must not be tracked: {relative}")
        if path.suffix.lower() in PROHIBITED_TRACKED_SUFFIXES:
            failures.append(f"binary media/model artifact requires manifest review: {relative}")
        size = path.stat().st_size
        if size > 5 * 1024 * 1024:
            failures.append(f"file exceeds bootstrap 5 MiB review threshold: {relative}")
        if size <= 5 * 1024 * 1024:
            text = path.read_bytes().decode("utf-8", errors="ignore")
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(text):
                    failures.append(f"possible {label} in {relative}; inspect without printing it")
            if path.suffix.lower() == ".md":
                check_markdown_links(path, failures)

    workflows = sorted((ROOT / ".github/workflows").glob("*.y*ml"))
    for workflow_path in workflows:
        workflow = workflow_path.read_text(encoding="utf-8")
        for action in ACTION_USE.findall(workflow):
            if not FULL_SHA_ACTION.fullmatch(action):
                failures.append(
                    f"GitHub Action is not pinned to a full SHA in "
                    f"{workflow_path.relative_to(ROOT)}: {action}"
                )
        if "pull_request_target" in workflow:
            failures.append(
                f"workflow must not use pull_request_target: {workflow_path.relative_to(ROOT)}"
            )

    history = subprocess.run(
        ["git", "log", "--all", "-p", "--full-history"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        errors="ignore",
    ).stdout
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(history):
            failures.append(f"possible {label} in Git history; inspect without printing it")

    diff_check = subprocess.run(
        ["git", "diff", "--check"], cwd=ROOT, capture_output=True, text=True
    )
    if diff_check.returncode:
        failures.append("git diff --check failed")

    if failures:
        print("Repository validation failed:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1

    print(f"Repository validation passed ({len(files)} tracked/candidate files checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
