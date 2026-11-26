"""Heuristic branch and session-title names for new worktrees.

Replaces the choices the orchestrator LLM made when spawning. The engine's spawn
path calls choose_names(), the one seam a model call could later replace. This
module does not import scan, so it stays stdlib-only and testable on its own.
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
# Letters NFKD cannot split into ASCII, folded before the ASCII step (text is
# lowercased first, so these keys cover the uppercase forms too).
FOLD = str.maketrans({"ß": "ss", "ø": "o", "ł": "l", "æ": "ae", "đ": "d"})


def slugify(text, maxlen=40):
    """Lowercase ASCII words joined by "-", cut at a word boundary within maxlen."""
    folded = unicodedata.normalize("NFKD", text.lower().translate(FOLD))
    slug = re.sub(r"[^a-z0-9]+", "-", folded.encode("ascii", "ignore").decode()).strip("-")
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


def title_from_branch(branch, strip_number=False):
    """feat/vertical-login-1756 -> "Vertical Login" with strip_number=True.
    Only issue branches end in their number; elsewhere the digits are words."""
    name = branch.split("/", 1)[-1]
    if strip_number:
        name = re.sub(r"-\d+$", "", name)
    return " ".join(w[:1].upper() + w[1:] for w in name.split("-") if w)


def issue_names(number, title, labels=()):
    """-> (branch, title) for an issue: <type>/<slug>-<number>.

    Not deduplicated: an issue's branch is unique by its number, and after a
    detach the same branch and worktree come back on purpose (add_worktree
    reuses them). Do not pass it through unique_branch.
    """
    kind = branch_type(title, labels)
    _, body = _split_prefix(title)
    branch = f"{kind}/{slugify(body) or 'issue'}-{number}"
    return branch, title_from_branch(branch, strip_number=True)


def _investigation_parts(question, read_only):
    """-> (type, base slug). A read-only question is always a chore: it commits nothing."""
    if read_only:
        kind = "chore"
    elif FIX_WORDS.search(question):
        kind = "fix"
    elif DOC_WORDS.search(question):
        kind = "docs"
    else:
        kind = "chore"
    return kind, slugify(question, 32) or "investigation"


def _git_ref_exists(repo_path, branch):
    """Local refs/heads and refs/remotes/origin. origin refs are only as current
    as the last `git fetch`."""
    for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        done = subprocess.run(["git", "-C", repo_path, "show-ref", "--verify", "--quiet", ref],
                              capture_output=True)
        if done.returncode == 0:
            return True
    return False


def _worktree_dir(repo_path, branch):
    """Mirrors scan.worktree_dir, which names a worktree directory."""
    return os.path.join(repo_path.rstrip("/"), ".worktrees", branch.replace("/", "-"))


def unique_branch(repo_path, branch, taken=(), ref_exists=_git_ref_exists):
    """branch, or branch-2, branch-3... until no ref, worktree directory or name
    in `taken` has it. ref_exists(repo_path, branch) -> bool; it reads local refs,
    so origin refs are only current after a fetch. For investigations only: issue
    branches are not deduplicated (see issue_names)."""
    candidate, n = branch, 2
    while (candidate in taken or ref_exists(repo_path, candidate)
           or os.path.isdir(_worktree_dir(repo_path, candidate))):
        candidate = f"{branch}-{n}"
        n += 1
    return candidate


def pick_investigation(question, read_only, repo_path, repo_slug,
                       taken_keys=(), taken_branches=(), ref_exists=_git_ref_exists):
    """-> (name, branch, title) for a question that is not yet an item.

    The name is the branch's slug, so it moves with the branch when the branch
    is taken (x -> x-2). It also moves past any key `<repo_slug>?<name>` already
    in taken_keys: spawn_item overwrites an item with the same key.
    """
    kind, base = _investigation_parts(question, read_only)
    branch = unique_branch(repo_path, f"{kind}/{base}", taken_branches, ref_exists)
    n = 2
    while f"{repo_slug}?{branch.split('/', 1)[1]}" in taken_keys:
        branch = unique_branch(repo_path, f"{kind}/{base}-{n}", taken_branches, ref_exists)
        n += 1
    return branch.split("/", 1)[1], branch, title_from_branch(branch)


def choose_names(kind, **ctx):
    """The one place that picks names: "issue" or "investigation".

    investigation needs question, repo_path and repo_slug; taken_keys,
    taken_branches and read_only are optional.
    """
    if kind == "issue":
        return issue_names(ctx["number"], ctx["title"], ctx.get("labels", ()))
    if kind == "investigation":
        return pick_investigation(ctx["question"], ctx.get("read_only", False),
                                  ctx["repo_path"], ctx["repo_slug"],
                                  ctx.get("taken_keys", ()), ctx.get("taken_branches", ()))
    raise ValueError(f"unknown naming kind: {kind!r}")
