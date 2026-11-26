"""Rules for which snapshot rows are still worth acting on.

A row is computed from the state at one scan. Actions taken after it (a
cleanup, a feedback mark, a decline) can make it stale, so the executor asks
`revalidate_row` before acting on each row. revalidate_row is pure (no I/O, no
imports from scan); deliver_pending does its I/O through scan.

Rows must come from the tick's saved scan, not from snapshot()'s copy: a
forward re-key (issue key -> PR key) is not mapped by prev_keys, so an issue
row cannot be followed to the PR it became.
"""
import re
import time
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Callable

# Row kinds the executor acts on, and the action each one becomes.
PLANNED = {"MERGED": "cleanup", "RESTACK": "restack", "REVIEW": "review",
           "BOOTING": "boot", "FEEDBACK": "feedback"}

Action = namedtuple("Action", "kind key data")

# Rows built from a tracked item. GONE is left out: its item was just dropped.
TRACKED = {"BOOTING", "CAPPED", "CLOSED", "DONE", "FEEDBACK", "MERGED", "PUSHED",
           "READY", "RESTACK", "REVIEW", "STALLED", "UNKNOWN", "WORKING"}


def find_item(state, key):
    """The item tracked under `key`, or under a key it was re-keyed from."""
    items = state.get("items", {})
    if key in items:
        return items[key]
    for it in items.values():
        if key in it.get("prev_keys", []):
            return it
    return None


def layer_keys(state):
    """Keys of the stack layers above each item's bottom."""
    out = set()
    for k, it in state.get("items", {}).items():
        slug = k.split("#")[0]
        out.update(f"{slug}#{layer['pr']}" for layer in it.get("stack") or [])
    return out


def revalidate_row(row, state):
    """True when the snapshot `row` still holds against `state`."""
    kind, key = row["kind"], row["key"]
    fields = row.get("fields", {})
    it = find_item(state, key)
    if kind in TRACKED and it is None:
        return False
    # A restack leaves the old key in handled_merges and keeps the item's
    # stack, so the merge check has to come before the stack check.
    if kind in ("MERGED", "RESTACK") and key in state.get("handled_merges", []):
        return False
    if kind == "RESTACK":
        return bool(it.get("stack"))
    if kind == "REVIEW":
        if it.get("phase") == "booting":
            return False
        # A re-request reboot() already acted on has run its cycle: its
        # mark-reviewed sets reviewed_sha to this same head, so the check below
        # would keep the row.
        head = fields.get("head")
        if head and (it.get("rerequest_head") or "").startswith(head):
            return False
        # mark-reviewed since the row was built changes reviewed_sha. Comparing
        # head would drop a fresh re-request, whose head equals reviewed_sha.
        # Rows without the field (or empty) are kept.
        built = fields.get("reviewed")
        now = (it.get("reviewed_sha") or "none")[:7]
        return not built or built == now
    if kind == "BOOTING":
        return it.get("phase") == "booting"
    if kind == "FEEDBACK":
        # Same watermark as scan.layer_since: a layer's own, else the item's.
        pr = fields.get("pr")
        seen = (it.get("layer_seen") or {}).get(str(pr)) if pr else None
        seen = seen or it.get("feedback_seen")
        return fields.get("since", "") > (seen or "")
    if kind == "PROPOSE":
        return (it is None and key not in state.get("declined", [])
                and key not in layer_keys(state))
    return True


def plan_rows(rows, state):
    """The actions for the rows still valid against `state`, in row order.

    Every kind not in PLANNED is left to the user, CAPPED included. Nothing
    is planned while paused. At most one action per key.
    """
    if state.get("paused"):
        return []
    out = []
    seen = set()
    # Restacks go first: a merge's fan-out must reach a stack after its restack
    # prompt (the one carrying RESTACK_FROM), not before it. A stable sort.
    rows = sorted(rows, key=lambda r: r["kind"] != "RESTACK")
    for row in rows:
        kind, key = row["kind"], row["key"]
        if kind not in PLANNED or key in seen:
            continue
        fields = row.get("fields", {})
        # Comment-mode items get their follow-up from the review, so their
        # FEEDBACK row is skipped rather than asserted: an assert would crash
        # the tick, and vanishes under python -O.
        if kind == "FEEDBACK" and fields.get("mode") == "comment":
            continue
        if not revalidate_row(row, state):
            continue
        if kind == "BOOTING":
            data = {"template": fields.get("template"), "prev": fields.get("PREV")}
        elif kind == "FEEDBACK":
            data = {"since": fields.get("since"), "pr": fields.get("pr"),
                    "branch": fields.get("branch")}
        else:
            data = {}
        seen.add(key)
        out.append(Action(PLANNED[kind], key, data))
    return out


# Outcomes kept on a pending send's `error`: a blocked entry is logged once.
BLOCKED = "agent blocked"
FAILED = "send failed"

# No action starts this long after the act phase began. Invariant: budget + the
# slowest action (a review, 750s: /clear 120, idle 90, pr_detail 60, 2 fetches,
# 3 git calls of 60, send 120) + the scan < the dashboard's 1200s tick timeout.
ACT_BUDGET_SECS = 240

# The engine's own git fetch limit: long enough for a big first fetch.
FETCH_TIMEOUT = 90

# A queued prompt older than this is stale: the world it was written for moved.
QUEUE_MAX_AGE_SECS = 24 * 3600

# Failed tries after which a queued send is dropped and the item told.
MAX_TRIES = 5


@dataclass
class Ctx:
    """One run of the executor. `sent_keys` holds the identities (sessions) of
    the items already sent to or queued for in this run, so each item gets at
    most one prompt per run."""
    dry_run: bool
    sent_keys: set
    log: Callable
    rows: list
    deadline: float = None          # clock() reading past which nothing starts
    clock: Callable = time.monotonic
    merging: frozenset = frozenset()  # keys whose cleanup is planned this run
    restacked: set = field(default_factory=set)  # identities already covered


def _over_budget(ctx):
    return ctx.deadline is not None and ctx.clock() >= ctx.deadline


def _identity(state, key):
    """What names one item across re-keys: its session, else its current key."""
    it = find_item(state, key)
    if it is None:
        return key
    if it.get("session"):
        return it["session"]
    return next(k for k, v in state.get("items", {}).items() if v is it)


def queue_send(state, key, template, subs, reason, mark=None):
    """Queue a prompt for `key`. It replaces a queued prompt for the same item
    with the same reason only, so a rebase is never lost to a feedback prompt.
    `mark` is applied once the prompt has gone out."""
    ident = _identity(state, key)
    sends = [p for p in state.get("pending_sends", [])
             if not (p.get("reason") == reason
                     and _identity(state, p["key"]) == ident)]
    entry = {"key": key, "template": template, "subs": subs,
             "at": int(time.time()), "tries": 0, "error": None,
             "reason": reason}
    if mark:
        entry["mark"] = mark
    sends.append(entry)
    state["pending_sends"] = sends


def _failed_try(state, ctx, entry, key, out):
    """Count a failed try. After MAX_TRIES the entry is dropped, so it cannot
    hold the item's queue for good, and the item says why (logged once)."""
    entry["tries"] += 1
    if entry["tries"] < MAX_TRIES:
        return
    state["pending_sends"] = [p for p in state["pending_sends"] if p is not entry]
    ctx.log("send-dropped", key, template=entry["template"], tries=entry["tries"])
    _note(state, ctx, key, "engine_error", "engine-error",
          f"{entry['reason']} dropped: {entry['template']} failed "
          f"{entry['tries']} times", action=entry["reason"])
    out.append(f"{key}: dropped after {entry['tries']} tries")


def _stale(entry, it):
    """Why a queued prompt no longer holds for its item, or None. The queue can
    outlive the state it was written for (a review that ran, feedback marked)."""
    import scan
    reason = entry["reason"]
    # A rebase is rendered against the base when it goes out: it never ages.
    if reason not in ("rebase", "restack") \
            and time.time() - entry["at"] > QUEUE_MAX_AGE_SECS:
        return "older than a day"
    if reason == "boot":
        if it.get("phase") != "booting":
            return "item is no longer booting"
        if int(it.get("review_rounds", 0)) >= scan.MAX_REVIEW_ROUNDS:
            return "review cap reached"
    if reason == "feedback" and (entry.get("mark") or {}).get("op") == "feedback":
        mark = entry["mark"]
        if mark["since"] <= (scan.layer_since(it, mark.get("pr")) or ""):
            return "feedback already handled"
    return None


def deliver_pending(ctx):
    """Try each queued send once, in queue order, one per item per run.
    Returns one outcome line per entry tried. Each entry takes the state lock
    itself, so the caller must not hold it (it is not re-entrant)."""
    # Imported late: scan will import engine for the tick.
    import scan
    out = []
    refs = [(p["key"], p["reason"], p["at"])
            for p in scan.load().get("pending_sends", [])]
    for i, (key, reason, at) in enumerate(refs):
        if _over_budget(ctx):
            out.append(f"budget spent: {len(refs) - i} queued send(s) left "
                       "for the next tick")
            break
        with scan.locked():
            state = scan.load()
            # A pause set mid-run takes effect at once; queued prompts wait.
            if state.get("paused"):
                out.append("paused: queued sends wait")
                break
            entry = next((p for p in state.get("pending_sends", [])
                          if (p["key"], p["reason"], p["at"]) == (key, reason, at)),
                         None)
            if entry is None:
                continue
            # The item may have been re-keyed since queueing: the executor
            # uses the item's current key and identity.
            resolved = scan.resolve_key(state, key)
            ident = _identity(state, key)
            if ident in ctx.sent_keys:
                continue
            # send_item exits on an unknown item, so check before sending.
            if resolved not in state.get("items", {}):
                if ctx.dry_run:
                    out.append(f"{key}: would drop, item gone")
                    continue
                state["pending_sends"] = [p for p in state["pending_sends"]
                                          if p is not entry]
                scan.save(state)
                ctx.log("send-dropped", key, template=entry["template"])
                out.append(f"{key}: dropped, item gone")
                continue
            why = _stale(entry, state["items"][resolved])
            if why:
                if ctx.dry_run:
                    out.append(f"{key}: would drop, {why}")
                    continue
                state["pending_sends"] = [p for p in state["pending_sends"]
                                          if p is not entry]
                scan.save(state)
                ctx.log("send-dropped", key, template=entry["template"], why=why)
                if why == "older than a day":
                    # A one-shot prompt was lost, not made moot: say so.
                    _note(state, ctx, resolved, "engine_error", "engine-error",
                          f"{reason} dropped: {entry['template']} {why}",
                          action=reason)
                    scan.save(state)
                out.append(f"{key}: dropped, {why}")
                continue
            ctx.sent_keys.add(ident)
            if ctx.dry_run:
                out.append(f"{key}: would send {entry['template']}")
                continue
            # A crash here must not wedge the queue: the entry counts as a
            # failed try and the next entry still goes out.
            try:
                text = scan.render_for(state, entry["template"], entry["subs"])
                code = scan.send_item(state, resolved, text)
            except (Exception, SystemExit) as exc:
                # Nothing went out, so the item's slot for this run is free.
                ctx.sent_keys.discard(ident)
                entry["error"] = f"{FAILED}: {type(exc).__name__}: {exc}"
                out.append(f"{key}: failed, try {entry['tries'] + 1}")
                _failed_try(state, ctx, entry, resolved, out)
                scan.save(state)
                continue
            if code == 0:
                _clear_notes(state["items"][resolved])
                if entry.get("mark"):
                    _apply_mark(state, resolved, entry["mark"])
                state["pending_sends"] = [p for p in state["pending_sends"]
                                          if p is not entry]
                out.append(f"{key}: sent")
            elif code == 4:
                if entry["error"] != BLOCKED:
                    _note(state, ctx, resolved, "engine_wait", "send-waiting",
                          f"waiting: {entry['template']}, the agent is on an "
                          "approval or question")
                entry["error"] = BLOCKED
                out.append(f"{key}: blocked, kept for retry")
            else:
                entry["error"] = FAILED
                out.append(f"{key}: failed, try {entry['tries'] + 1}")
                _failed_try(state, ctx, entry, resolved, out)
            scan.save(state)
    return out


# ------------------------------------------------------------------ executor
#
# Each step takes the state lock on its own and re-reads the state: a reboot,
# a checkout or a send in between is slow, and another writer may move the
# item meanwhile. Nothing here merges or pushes.

def _apply_mark(state, key, mark):
    """The state change a prompt stood for, made once the prompt has gone out:
    a review counted (mark_reviewed) or a feedback watermark moved."""
    import scan
    if mark["op"] == "reviewed":
        return scan.mark_reviewed(state, key, mark["sha"])
    return scan.mark_feedback(state, key, mark["since"], mark["pr"])


def _note(state, ctx, key, field, event, msg, **fields):
    """Keep `msg` on the item under `field`, where the card shows it, and log
    `event` only when the message is new for that item, so a stuck item logs
    once rather than every tick. Caller holds the lock and saves."""
    it = find_item(state, key)
    if it is not None:
        if it.get(field) == msg:
            return
        it[field] = msg
        if field == "engine_error":
            it["last_note"] = msg
    ctx.log(event, key, msg=msg, **fields)


def _record(ctx, key, field, event, msg, **fields):
    """_note on its own: takes the lock, loads and saves."""
    import scan
    with scan.locked():
        state = scan.load()
        _note(state, ctx, key, field, event, msg, **fields)
        scan.save(state)


def _fail(ctx, key, action, error):
    """An action that could not be done: the reason goes on the item."""
    _record(ctx, key, "engine_error", "engine-error",
            f"{action} failed: {error}", action=action)
    return f"{key}: {action} failed: {error}"


def _clear_notes(it):
    """A prompt went out: the item's failure and wait notes no longer hold."""
    if it.get("engine_error") and it.get("last_note") == it["engine_error"]:
        it.pop("last_note")
    it.pop("engine_error", None)
    it.pop("engine_wait", None)


def _send_in(state, ctx, key, template, subs, reason, mark=None):
    """Send `template` to the item under `key`, or queue it when it cannot go
    out now: its agent is busy on an approval (exit 4, logged once as waiting),
    the send failed (logged as an error), or the item already got a prompt this
    run. Caller holds the lock and passes the state it loaded."""
    import scan
    k = scan.resolve_key(state, key)
    it = find_item(state, k)
    if it is None:
        return f"{key}: not tracked, nothing sent"
    ident = _identity(state, k)
    if ident in ctx.sent_keys:
        queue_send(state, key, template, subs, reason, mark)
        scan.save(state)
        return f"{k}: {template} queued, sent next run"
    ctx.sent_keys.add(ident)
    try:
        text = scan.render_for(state, template, dict(subs))
        code = scan.send_item(state, k, text)
    except (Exception, SystemExit) as exc:
        # Nothing went out: queue it, free the item's slot, and let a fan-out
        # go on to the next sibling.
        ctx.sent_keys.discard(ident)
        queue_send(state, key, template, subs, reason, mark)
        _note(state, ctx, k, "engine_error", "engine-error",
              f"{reason} failed: {type(exc).__name__}: {exc}", action=reason)
        scan.save(state)
        return f"{k}: queued {template}, send raised"
    if code != 0:
        queue_send(state, key, template, subs, reason, mark)
        if code == 4:
            _note(state, ctx, k, "engine_wait", "send-waiting",
                  f"waiting: {template}, the agent is on an approval or question")
        else:
            _note(state, ctx, k, "engine_error", "engine-error",
                  f"{reason} failed: the send to the agent failed", action=reason)
        scan.save(state)
        return f"{k}: queued {template}, kept for retry"
    # A fresh prompt supersedes a queued one with the same reason, so that one
    # must not go out later as a stale duplicate.
    state["pending_sends"] = [p for p in state.get("pending_sends", [])
                              if not (p.get("reason") == reason
                                      and _identity(state, p["key"]) == ident)]
    _clear_notes(it)
    if mark:
        _apply_mark(state, k, mark)
    scan.save(state)
    return f"{k}: sent {template}"


def _send(ctx, key, template, subs, reason, mark=None):
    import scan
    with scan.locked():
        return _send_in(scan.load(), ctx, key, template, subs, reason, mark)


def _rebase_prompt(slug, base, key, it):
    """The prompt that rebases a fan-out item onto its base after a merge."""
    if it.get("stack"):
        return "rebase-stack", {"REPO": slug, "BASE": base, "KEY": key}
    return "rebase", {"REPO": slug, "PR": it["pr"], "BRANCH": it["branch"],
                      "BASE": base}


def _fan_out(ctx, slug, base, siblings):
    """Rebase prompts for the siblings, except those the restack prompt already
    covers (it rebases onto the current base, merge included) and items whose
    own merge is cleaned up this run."""
    import scan
    out = []
    for k, it in siblings:
        state = scan.load()
        if k in ctx.merging or _identity(state, k) in ctx.restacked:
            continue
        template, subs = _rebase_prompt(slug, base, k, it)
        if _over_budget(ctx):
            # The merge is already handled: what is left is queued, not lost.
            with scan.locked():
                state = scan.load()
                queue_send(state, k, template, subs, "rebase")
                scan.save(state)
            out.append(f"{k}: {template} queued, out of time")
        else:
            out.append(_send(ctx, k, template, subs, "rebase"))
        # Sent or queued, it rebases onto the base with every merge so far, so
        # a second merge in this run needs no second prompt for the session.
        ctx.restacked.add(_identity(scan.load(), k))
    return out


def checkout_pr_head(wt, pr, branch):
    """Detach the worktree `wt` on the head of PR `pr`. -> (sha, note): sha is
    the head it now sits on, or None with the reason in note. The worktree is
    never moved over uncommitted changes."""
    import scan
    if not wt:
        return None, "no worktree for the item"
    code, _, err = scan.sh(["git", "-C", wt, "fetch", "origin", f"pull/{pr}/head"],
                           timeout=FETCH_TIMEOUT)
    if code != 0:
        # The PR's own ref can be missing (a fork); its branch may still be there.
        code, _, err = scan.sh(["git", "-C", wt, "fetch", "origin", branch],
                               timeout=FETCH_TIMEOUT)
    if code != 0:
        return None, f"fetch failed: {err}"
    _, sha, _ = scan.sh(["git", "-C", wt, "rev-parse", "FETCH_HEAD"])
    if not sha:
        return None, "no FETCH_HEAD"
    code, dirty, err = scan.sh(["git", "-C", wt, "status", "--porcelain"])
    if code != 0:
        return None, f"git status failed: {err}"
    if dirty:
        return None, "worktree has uncommitted changes"
    code, _, err = scan.sh(["git", "-C", wt, "checkout", "--detach", "FETCH_HEAD"])
    if code != 0:
        return None, f"checkout failed: {err}"
    return sha, ""


def _worktree(it):
    """The item's worktree, else its session's folder (items added by hand
    with `track` have none recorded)."""
    if it.get("worktree"):
        return it["worktree"]
    import scan
    sessions, _, _ = scan.all_sessions()
    for s in sessions:
        if s.get("id") == it.get("session"):
            return s.get("path")
    return None


def _cleanup(ctx, key):
    import scan
    with scan.locked():
        state = scan.load()
        r = scan.cleanup_item(state, scan.resolve_key(state, key))
    if not r["ok"]:
        return _fail(ctx, key, "cleanup", r["error"])
    slug = scan.split_key(key)[0]
    sent = _fan_out(ctx, slug, r["base"], r["siblings"])
    if r["discard_error"]:
        _record(ctx, key, "engine_error", "engine-error",
                f"cleanup: removing the workspace failed: {r['discard_error']}",
                action="cleanup")
    return "; ".join([f"{key}: cleaned up"] + sent)


def _restack(ctx, key):
    import scan
    with scan.locked():
        r = scan.restack_item(scan.load(), key)
    if not r["ok"]:
        return _fail(ctx, key, "restack", r["error"])
    slug = scan.split_key(key)[0]
    with scan.locked():
        ctx.restacked.add(_identity(scan.load(), r["new_key"]))
    sent = [_send(ctx, r["new_key"], r["template"], r["subs"], "restack")]
    sent += _fan_out(ctx, slug, r["base"], r["siblings"])
    return "; ".join(sent)


def _review(ctx, key):
    import scan
    with scan.locked():
        state = scan.load()
        k = scan.resolve_key(state, key)
        it = state.get("items", {}).get(k)
        if it is None:
            return f"{key}: not tracked, nothing to review"
        rounds = int(it.get("review_rounds", 0))
        if rounds >= scan.MAX_REVIEW_ROUNDS:
            ctx.log("engine-skip", k, reason="review cap reached", rounds=rounds)
            return f"{k}: capped at {rounds}, skipped"
        # A prompt already went to this agent this run: a /clear now would wipe
        # it. The REVIEW row is still there next tick.
        if _identity(state, k) in ctx.sent_keys:
            return f"{k}: review deferred, the agent got a prompt this run"
        code = scan.reboot(state, k)
    if code == 0:
        return _boot(ctx, k)
    if code == 3:
        return f"{k}: rebooting, not idle yet; booting next tick"
    if code == 4:
        msg = "waiting: reboot, the agent is on an approval or question"
        _record(ctx, k, "engine_wait", "send-waiting", msg)
        return f"{k}: {msg}"
    return _fail(ctx, k, "review", f"reboot exit {code}")


def _boot(ctx, key):
    import scan
    with scan.locked():
        state = scan.load()
        k = scan.resolve_key(state, key)
        it = state.get("items", {}).get(k)
        if it is None or it.get("phase") != "booting":
            return f"{key}: not booting, nothing sent"
        me = scan.whoami(state)
    slug = scan.split_key(k)[0]
    pr, branch = it.get("pr"), it.get("branch")
    # gh and git are slow: neither runs under the lock.
    d = scan.pr_detail(slug, pr) if pr else None
    if d is None:
        return _fail(ctx, k, "boot", "PR detail unavailable")
    template, prev = scan.boot_template(it, d, me)
    head, note = checkout_pr_head(_worktree(it), pr, branch)
    if head is None:
        # Not sent: a review of a worktree that is not on the PR's head would
        # be a review of the wrong code. The row is still BOOTING next tick.
        return _fail(ctx, k, "boot", note)
    with scan.locked():
        state = scan.load()
        k = scan.resolve_key(state, k)
        if state.get("items", {}).get(k, {}).get("phase") != "booting":
            return f"{k}: no longer booting, nothing sent"
        base = state.get("registry", {}).get(slug, {}).get("base", "main")
        subs = {"REPO": slug, "PR": pr, "BRANCH": branch, "BASE": base}
        if template == "re-review":
            subs["PREV"] = prev
        return _send_in(state, ctx, k, template, subs, "boot",
                        mark={"op": "reviewed", "sha": head})


def _feedback(ctx, key, data):
    import scan
    with scan.locked():
        state = scan.load()
        k = scan.resolve_key(state, key)
        it = state.get("items", {}).get(k)
        if it is None:
            return f"{key}: not tracked, nothing sent"
        slug = scan.split_key(k)[0]
        base = state.get("registry", {}).get(slug, {}).get("base", "main")
        pr = data.get("pr")
        if pr:
            # A stack: the layer's PR and branch, with the stack's key.
            pr = int(pr)
            subs = {"REPO": slug, "PR": pr, "BRANCH": data["branch"],
                    "BASE": base, "KEY": k}
        else:
            pr = None
            subs = {"REPO": slug, "PR": it["pr"], "BRANCH": it["branch"],
                    "BASE": base}
        mark = {"op": "feedback", "since": data["since"], "pr": pr}
        return _send_in(state, ctx, k, "address-feedback", subs, "feedback",
                        mark=mark)


def execute(action, ctx):
    """Act on one planned action -> one outcome line. An action that fails is
    recorded on its item and returned, never raised, so the next one still
    runs. Under dry_run it changes nothing."""
    if ctx.dry_run:
        return f"{action.key}: would {action.kind}"
    try:
        if action.kind == "cleanup":
            return _cleanup(ctx, action.key)
        if action.kind == "restack":
            return _restack(ctx, action.key)
        if action.kind == "review":
            return _review(ctx, action.key)
        if action.kind == "boot":
            return _boot(ctx, action.key)
        if action.kind == "feedback":
            return _feedback(ctx, action.key, action.data)
        raise ValueError(f"no executor for {action.kind}")
    except (Exception, SystemExit) as exc:
        return _fail(ctx, action.key, action.kind,
                     f"{type(exc).__name__}: {exc}")


def _deliver_code():
    """Deliver the code-changes requests the dashboard queued, as `scan` does
    after its own scan: under the state lock, so each goes out once."""
    import scan
    with scan.locked():
        state = scan.load()
        if not state.get("code_pending"):
            return []
        sent, err = scan.send_code(state)
    return [f"{scan.CODE_AGENT}: {sent} request(s) delivered"
            + (f", still pending ({err})" if err else "")]


def _record_skip(cut):
    """Log `engine-skip` when the budget cut work, once per change of what was
    cut (like engine_plan): the dashboard runs the tick quiet."""
    import scan
    with scan.locked():
        state = scan.load()
        if cut == (state.get("engine_skip") or []):
            return
        state["engine_skip"] = cut
        scan.save(state)
    if cut:
        scan.log_event("engine-skip", "budget", reason="act budget spent",
                       left=", ".join(cut))


def run_rows(rows, mode, started, clock=time.monotonic):
    """Plan the tick's `rows` (scan's (kind, key, extra) tuples) and, in on
    mode, act on the plan. Plan mode logs what would happen, when the planned
    set changes, and changes nothing else. Starts no action once ACT_BUDGET_SECS
    past `started` (a clock() reading taken when the act phase began).
    Caller holds exec.lock, not the state lock. -> the outcome lines."""
    import scan
    state = scan.load()
    dict_rows = [{"kind": kind, "key": key,
                  "fields": scan.parse_extra(extra)["fields"]}
                 for kind, key, extra in sorted(rows, key=scan.row_order)]
    dry = mode == "plan"
    plan = plan_rows(dict_rows, state)
    ctx = Ctx(dry_run=dry, sent_keys=set(), log=scan.log_event, rows=dict_rows,
              deadline=started + ACT_BUDGET_SECS, clock=clock,
              merging=frozenset(a.key for a in plan if a.kind == "cleanup"))
    if dry:
        planned = [f"{a.key} {a.kind}" for a in plan]
        if planned != (state.get("engine_plan") or []):
            with scan.locked():
                state = scan.load()
                state["engine_plan"] = planned
                scan.save(state)
            for a in plan:
                scan.log_event("engine-plan", a.key, action=a.kind)
        return [f"{a.key}: would {a.kind}" for a in plan]
    lines = []
    cut = []
    for i, action in enumerate(plan):
        if scan.load().get("paused"):
            lines.append(f"paused: {len(plan) - i} action(s) left")
            break
        if _over_budget(ctx):
            lines.append(f"budget spent: {len(plan) - i} action(s) left for the next tick")
            cut = [f"{a.key} {a.kind}" for a in plan[i:]]
            break
        lines.append(execute(action, ctx))
    delivered = deliver_pending(ctx)
    if any(line.startswith("budget spent") for line in delivered):
        cut.append("queued sends")
    _record_skip(cut)
    lines += delivered
    lines += _deliver_code()
    return lines


# ------------------------------------------------------------------ requests
#
# One request, as the prompter and the dashboard queue it (the inbox's
# `REQUEST <text>`, without the prefix), or one agent report line.

Request = namedtuple("Request", "verb args confirmed over_cap")

# Appended by the kitchen-closed dialog and by the prompter, after the user
# confirmed a request past wind-down or over MAX_ACTIVE.
CONFIRMED_PAST = "(confirmed past wind-down)"
CONFIRMED_OVER = "(confirmed over cap)"

ONE_TARGET = ("go", "no", "detach", "re-review", "ack")
NO_ARGS = ("pause", "resume", "tick")
INVESTIGATE = re.compile(r"/investigate\s+(--read-only\s+)?(\S+)\s+(.+)", re.DOTALL)
CODE = re.compile(r"/octopilot-code\s+(.+)", re.DOTALL)


def _strip_confirmations(text):
    """-> (text, confirmed past wind-down, confirmed over cap). Only markers at
    the very end count: one inside a question is part of the question."""
    confirmed = over_cap = False
    text = text.rstrip()
    while text.endswith((CONFIRMED_PAST, CONFIRMED_OVER)):
        if text.endswith(CONFIRMED_PAST):
            confirmed = True
            text = text[:-len(CONFIRMED_PAST)]
        else:
            over_cap = True
            text = text[:-len(CONFIRMED_OVER)]
        text = text.rstrip()
    return text, confirmed, over_cap


def parse_request(text):
    """A queued request or report line -> Request. Anything outside the grammar
    is verb "unknown" with the text as given, for the caller to refuse."""
    unknown = Request("unknown", (text,), False, False)
    line = text.strip()
    # A report's message is the agent's own text: no confirmation markers.
    if line.startswith("OCTOPILOT "):
        parts = line.split(maxsplit=4)
        if len(parts) < 4:
            return unknown
        msg = parts[4] if len(parts) > 4 else ""
        return Request("report", (parts[1], parts[2], parts[3], msg), False, False)
    line, confirmed, over_cap = _strip_confirmations(line)
    m = INVESTIGATE.fullmatch(line)
    if m:
        read_only, repo, question = m.groups()
        verb = "investigate-read-only" if read_only else "investigate"
        return Request(verb, (repo, question), confirmed, over_cap)
    m = CODE.fullmatch(line)
    if m:
        return Request("code", (m.group(1),), confirmed, over_cap)
    words = line.split(maxsplit=1)
    if not words:
        return unknown
    verb, rest = words[0], (words[1] if len(words) > 1 else "")
    if verb in ONE_TARGET and len(rest.split()) == 1:
        return Request(verb, (rest,), confirmed, over_cap)
    if verb in NO_ARGS and not rest:
        return Request(verb, (), confirmed, over_cap)
    return unknown
