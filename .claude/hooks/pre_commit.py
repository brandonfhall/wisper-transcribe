"""Claude Code PreToolUse hook: enforce the Definition of Done on `git commit`.

Blocks the commit when tailwind.min.css is stale/unstaged or tests fail;
warns (without blocking) when src/ changes ship without doc changes.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from wisper_transcribe.tailwind import build_css  # noqa: E402

CSS = "src/wisper_transcribe/static/tailwind.min.css"
DOC_PATHS = ("architecture.md", "README.md", "docs/", "plan.md")
COMMIT_RE = re.compile(r"\bgit\b(?:\s+-[Cc]\s+\S+)*\s+commit\b(.*)")


def run(*args, timeout=None):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def main():
    payload = json.load(sys.stdin)
    command = (payload.get("tool_input") or {}).get("command") or ""
    match = COMMIT_RE.search(command)
    if not match:
        return
    commit_all = re.search(r"(?:^|\s)(?:-a\b|--all\b|-\w*a\w*m\b)", match.group(1)) is not None

    problems = []

    build = build_css()
    if build.returncode != 0:
        problems.append(f"Tailwind rebuild failed:\n{build.stderr[-1500:]}")
    elif not commit_all and run("git", "diff", "--quiet", "--", CSS).returncode != 0:
        problems.append(f"Tailwind rebuild changed {CSS} — `git add` it and retry the commit.")

    tests = run(sys.executable, "-m", "pytest", "tests/", "-x", "-q", "--no-header", "-p", "no:cacheprovider",
                timeout=280)
    if tests.returncode != 0:
        problems.append(f"Tests failed — fix before committing:\n{tests.stdout[-3000:]}")

    if problems:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "\n\n".join(problems),
        }}))
        return

    diff_args = ("git", "diff", "--name-only", "HEAD") if commit_all else ("git", "diff", "--cached", "--name-only")
    changed = run(*diff_args).stdout.split()
    if any(f.startswith("src/") for f in changed) and not any(f.startswith(DOC_PATHS) for f in changed):
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "This commit changes src/ but no docs (architecture.md, docs/, README.md, plan.md). "
                                 "Confirm per CLAUDE.md Documentation Rules that no doc update is needed.",
        }}))


if __name__ == "__main__":
    main()
