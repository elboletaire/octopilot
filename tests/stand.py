"""The stand-ins the request tests share: GitHub, herdr and git faked through
fake_world, a session stand-in for scan.spawn_item, and the log of the events a
request writes. Requests run through engine._handle, so a test reads the
outcome line, the state and the events the way the drain would see them."""
import contextlib
import io
import json
import time
import unittest
from unittest import mock

from tests.fake import fake_world
import scan
from engine import Ctx, _handle, parse_request

REPO = "owner/repo"
HEAD = "new5678aaa"


class FakeBackend:
    """Stands in for a herdr backend: addresses are herdr:<session>."""
    name = "herdr"
    prefix = "herdr:"
    remove_label = "removed"
    noun = "workspace"

    def target(self, it):
        return it["session"]

    def close(self, session):
        return 0, ""

    def discard(self, session, worktree):
        return 0, ""


BACKEND = FakeBackend()


class RequestCase(unittest.TestCase):
    """A scratch state, a fake GitHub with one repo cloned, and the calls recorded.
    Set the attributes a test changes before calling handle()."""

    def setUp(self):
        self.issue = {"number": 12, "title": "Add login", "labels": [{"name": "bug"}],
                      "body": "", "comments": []}
        self.prs = {31: {"headRefName": "feat/b", "headRefOid": HEAD,
                         "title": "Second layer"}}
        self.open_prs = [{"number": 31, "headRefName": "feat/b", "baseRefName": "main"}]
        self.behind = False
        self.reply = (True, None)     # what send_to answers
        self.mode, self.template, self.prev, self.warning = "fix", "work", None, None
        world = fake_world(
            backend_for=lambda session: BACKEND,
            send_to=lambda address, text: self.reply,
            gh_json=self.gh,
            pr_detail=lambda slug, number: self.prs.get(number),
            sh=self.git,
        )
        self.world = world.__enter__()
        self.addCleanup(world.__exit__, None, None, None)
        # A session needs a worktree and a herdr workspace: a stand-in tracks the
        # item as scan.spawn_item does, and the calls are recorded.
        patch = mock.patch.object(scan, "spawn_item", side_effect=self.spawn)
        self.spawn_item = patch.start()
        self.addCleanup(patch.stop)
        quiet = contextlib.ExitStack()
        quiet.enter_context(contextlib.redirect_stdout(io.StringIO()))
        quiet.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.addCleanup(quiet.close)
        self.logs = []
        self.save({})

    # ---- stand-ins

    def gh(self, args, cwd=None, default=None):
        if args[:2] == ["issue", "view"]:
            return self.issue
        if args[:2] == ["pr", "list"]:
            return self.open_prs
        return default

    def git(self, args, cwd=None, timeout=60):
        if "merge-base" in args:
            return (1 if self.behind else 0), "", ""
        if "rev-parse" in args:
            return 0, HEAD, ""
        return 0, "", ""

    def spawn(self, state, key_, branch, title, new_branch, question=None,
              read_only=False, stack_spec=None):
        """What scan.spawn_item tracks and returns, with no worktree behind it."""
        stack = None
        if stack_spec:
            stack = [{"pr": int(p), "branch": b} for p, b in
                     (layer.split(":", 1) for layer in stack_spec.split())]
        it = {"mode": self.mode, "branch": branch, "session": "w9",
              "worktree": "/wt/" + branch.replace("/", "-"), "phase": "working",
              "pr": int(key_.split("#")[1]) if "#" in key_ else None,
              "added": int(time.time())}
        if stack:
            it["stack"] = stack
        template = self.template
        if question is not None:
            it["question"] = question
            template = "investigate-readonly" if read_only else "investigate"
        if read_only:
            it["read_only"] = True
        state["items"][key_] = it
        scan.save(state)
        return {"ok": True, "error": None, "key": key_, "template": template,
                "base": "main", "slug": REPO, "prev": self.prev, "stack": stack,
                "warning": self.warning, "model": ""}

    def log(self, kind, key, **fields):
        self.logs.append((kind, key, fields))

    def ctx(self):
        return Ctx(dry_run=False, sent_keys=set(), log=self.log, rows=[])

    def save(self, items, **extra):
        state = {"items": items, "handled_merges": [], "pending_sends": [],
                 "declined": [], "registry": {REPO: {"path": "/nonexistent/repo",
                                                    "base": "main"}}}
        state.update(extra)
        scan.save(state)

    def snapshot(self, active=0, untracked=0, rows=()):
        with open(scan.SNAPSHOT_FILE, "w") as fh:
            json.dump({"active": active, "untracked": untracked,
                       "rows": list(rows)}, fh)

    def handle(self, text):
        """The outcome line of one request, as the drain would produce it."""
        return _handle(parse_request(text), self.ctx())

    def spawns(self):
        return [c.args for c in self.spawn_item.call_args_list]

    def addresses(self):
        return [args[0] for args, _ in self.world.named("send_to")]

    def texts(self):
        return [args[1] for args, _ in self.world.named("send_to")]
