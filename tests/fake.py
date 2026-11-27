"""A fake GitHub, herdr and git for scan.py tests.

fake_world(**answers) patches the scan.py functions that reach GitHub, herdr,
git or a backend. Each answer is the return value, or a callable taking the
call's arguments. Anything not given answers empty. Every call is recorded in
world.calls as (name, args, kwargs). The state, event and snapshot files go to
a fresh directory per world.
"""
import contextlib
import os
import shutil
import tempfile
from unittest import mock

from tests import SCRATCH  # noqa: F401  (imported first: sets the environment)
import scan

# Answers for the functions a test does not mention.
EMPTY = {
    "whoami": "octo-user",
    "all_sessions": ([], set(), set()),
    "all_live": {},
    "parked_agent": None,
    "build_registry": {},
    "home_prs": [],
    "home_issues": [],
    "search_prs": [],
    "org_issues": [],
    "pr_detail": None,
    "open_heads": [],
    "gh_json": lambda args, cwd=None, default=None: default,
    "sh": (0, "", ""),
    "send_to": (True, None),
    "backend_for": None,
    "record_launcher_sessions": None,
    "octopilot_closed": False,
    "octopilot_reopened": False,
    "terminal_state": "busy",
    "end_terminal": (0, ""),
    "labelled_terminals": [],
    "core_doc": {"spaces": []},
}


class World:
    def __init__(self):
        self.calls = []

    def named(self, name):
        """The arguments of every call to one fake, in order."""
        return [(args, kwargs) for n, args, kwargs in self.calls if n == name]


def _core_rpc(world, doc):
    """hc_rpc answering doc.get with the herdr-app document, nothing else."""
    return _fake(world, "hc_rpc", lambda method, params, timeout=10:
                 {"value": doc} if method == "doc.get" else None)


def _fake(world, name, answer):
    def fn(*args, **kwargs):
        world.calls.append((name, args, kwargs))
        if callable(answer):
            return answer(*args, **kwargs)
        return answer
    return fn


@contextlib.contextmanager
def fake_world(**answers):
    unknown = set(answers) - set(EMPTY)
    if unknown:
        raise TypeError(f"not a faked function: {sorted(unknown)}")
    world = World()
    files = tempfile.mkdtemp(prefix="world-", dir=SCRATCH)
    patches = {
        "STATE_FILE": os.path.join(files, "state.json"),
        "EVENTS_FILE": os.path.join(files, "events.jsonl"),
        "SNAPSHOT_FILE": os.path.join(files, "snapshot.json"),
        "TICK_LOCK": os.path.join(files, "tick.lock"),
        "CLOSED_FILE": os.path.join(files, "space-closed"),
        "EXEC_LOCK": os.path.join(files, "exec.lock"),
    }
    with contextlib.ExitStack() as stack:
        for name, value in patches.items():
            stack.enter_context(mock.patch.object(scan, name, value))
        for name, default in EMPTY.items():
            answer = answers.get(name, default)
            if name == "core_doc":
                stack.enter_context(mock.patch.object(
                    scan, "hc_rpc", _core_rpc(world, answer)))
                continue
            stack.enter_context(mock.patch.object(
                scan, name, _fake(world, name, answer)))
        yield world
    shutil.rmtree(files, ignore_errors=True)
