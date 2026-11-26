"""Heuristic branch and session-title names for new worktrees.

Replaces the choices the orchestrator LLM made when spawning. choose_names() is
the one seam a model call could later replace. Stdlib only, and no scan import:
scan.py imports this module, not the other way round.
"""
import os
import re
import subprocess
import unicodedata

TYPES = ("feat", "fix", "chore", "docs", "test", "refactor")
LABEL_TYPES = {
    "bug": "fix",
    "enhancement": "feat", "feature": "feat",
    "documentation": "docs", "docs": "docs",
    "dependencies": "chore", "chore": "chore",
    "test": "test", "tests": "test",
    "refactor": "refactor",
}
# A conventional prefix such as "fix(auth)!:". Only the known types count as
# one, so "Config: add x" keeps its word.
CONVENTIONAL = re.compile(r"^(\w+)(\([^)]*\))?!?:\s*")
FIX_WORDS = re.compile(
    r"\b(fix(es|ed|ing)?|bugs?|broken|errors?|crash(es|ed)?|fail(s|ed|ing)?)\b",
    re.IGNORECASE)
DOC_WORDS = re.compile(r"\b(docs?|documents?|documentation|readme)\b", re.IGNORECASE)


def slugify(text, maxlen=40):
    """Lowercase ASCII words joined by "-", cut at a word boundary within maxlen."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if len(slug) <= maxlen:
        return slug
    head = slug[:maxlen]
    # Cutting inside a word would leave a fragment, so drop it when possible.
    if slug[maxlen] != "-" and "-" in head:
        head = head.rsplit("-", 1)[0]
    return head.strip("-")


def _label_names(labels):
    """GitHub gives labels as {"name": ...} objects; plain strings also work."""
    return [l.get("name", "") if isinstance(l, dict) else str(l) for l in labels or ()]


def _split_prefix(title):
    """-> (type, body): the conventional type when the title has one, else (None, title)."""
    title = title.strip()
    m = CONVENTIONAL.match(title)
    if m and m.group(1).lower() in TYPES:
        return m.group(1).lower(), title[m.end():]
    return None, title


def branch_type(title, labels=()):
    """Labels first, then a conventional prefix, then the title's words, else feat."""
    for name in _label_names(labels):
        kind = LABEL_TYPES.get(name.strip().lower())
        if kind:
            return kind
    kind, body = _split_prefix(title)
    if kind:
        return kind
    if FIX_WORDS.search(body):
        return "fix"
    if DOC_WORDS.search(body):
        return "docs"
    return "feat"


def title_from_branch(branch):
    """feat/vertical-login-1756 -> "Vertical Login"."""
    name = re.sub(r"-\d+$", "", branch.split("/", 1)[-1])
    return " ".join(w[:1].upper() + w[1:] for w in name.split("-") if w)


def issue_names(number, title, labels=()):
    """-> (branch, title) for an issue: <type>/<slug>-<number>."""
    kind = branch_type(title, labels)
    _, body = _split_prefix(title)
    branch = f"{kind}/{slugify(body) or 'issue'}-{number}"
    return branch, title_from_branch(branch)


def investigation_names(question, read_only=False):
    """-> (name, branch, title) for a question. Read-only is accepted for the
    caller's symmetry; it does not change the name."""
    if FIX_WORDS.search(question):
        kind = "fix"
    elif DOC_WORDS.search(question):
        kind = "docs"
    else:
        kind = "chore"
    name = slugify(question, 32) or "investigation"
    branch = f"{kind}/{name}"
    return name, branch, title_from_branch(branch)


def choose_names(kind, **ctx):
    """The one place that picks names: "issue" or "investigation"."""
    if kind == "issue":
        return issue_names(ctx["number"], ctx["title"], ctx.get("labels", ()))
    if kind == "investigation":
        return investigation_names(ctx["question"], ctx.get("read_only", False))
    raise ValueError(f"unknown naming kind: {kind!r}")


def _worktree_dir(repo_path, branch):
    """Mirrors scan.worktree_dir, which names a worktree directory."""
    return os.path.join(repo_path.rstrip("/"), ".worktrees", branch.replace("/", "-"))


def _git_ref_exists(repo_path, branch):
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        done = subprocess.run(["git", "-C", repo_path, "show-ref", "--verify", "--quiet", ref],
                              capture_output=True)
        if done.returncode == 0:
            return True
    return False


def unique_branch(repo_path, branch, taken=(), ref_exists=_git_ref_exists):
    """branch, or branch-2, branch-3... until no local or remote ref, worktree
    directory or name in `taken` has it. ref_exists(repo_path, branch) -> bool."""
    candidate, n = branch, 2
    while (candidate in taken or ref_exists(repo_path, candidate)
           or os.path.isdir(_worktree_dir(repo_path, candidate))):
        candidate = f"{branch}-{n}"
        n += 1
    return candidate
