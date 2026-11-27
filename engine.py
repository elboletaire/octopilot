"""Rules for which snapshot rows are still worth acting on.

A row is computed from the state at one scan. Actions taken after it (a
cleanup, a feedback mark, a decline) can make it stale, so the executor asks
`revalidate_row` before acting on each row. revalidate_row is pure (no I/O, no
imports from scan); deliver_pending does its I/O through scan.

Rows must come from the tick's saved scan, not from snapshot()'s copy: a
forward re-key (issue key -> PR key) is not mapped by prev_keys, so an issue
row cannot be followed to the PR it became.
"""
import json
import re
import time
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Callable

import naming

# Row kinds the executor acts on, and the action each one becomes.
PLANNED = {"MERGED": "cleanup", "RESTACK": "restack", "REVIEW": "review",
           "BOOTING": "boot", "FEEDBACK": "feedback"}

Action = namedtuple("Action", "kind key data")

# Rows built from a tracked item. GONE is left out: its item was just dropped.
TRACKED = {"BLOCKED", "BOOTING", "CAPPED", "CLOSED", "DONE", "FEEDBACK", "HOLD",
           "MERGED", "PUSHED", "READY", "RESTACK", "REVIEW", "STALLED", "UNKNOWN",
           "WORKING"}


def locate_item(state, key):
    """(key, item) of the item tracked under `key` or a key it was re-keyed
    from, else (None, None)."""
    items = state.get("items", {})
    if key in items:
        return key, items[key]
    for k, it in items.items():
        if key in (it.get("prev_keys") or []):
            return k, it
    return None, None


def find_item(state, key):
    """The item tracked under `key`, or under a key it was re-keyed from."""
    return locate_item(state, key)[1]


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


def _send_in(state, ctx, key, template, subs, reason, mark=None, rendered=None):
    """Send `template` to the item under `key`, or queue it when it cannot go
    out now: its agent is busy on an approval (exit 4, logged once as waiting),
    the send failed (logged as an error), or the item already got a prompt this
    run. `rendered` is the prompt if the caller already rendered it. Caller holds
    the lock and passes the state it loaded."""
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
        text = rendered if rendered is not None \
            else scan.render_for(state, template, dict(subs))
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
        item_key = scan.resolve_key(state, key)
        sess = state.get("items", {}).get(item_key, {}).get("session")
        r = scan.cleanup_item(state, item_key)
    if not r["ok"]:
        return _fail(ctx, key, "cleanup", r["error"])
    slug = scan.split_key(key)[0]
    sent = _fan_out(ctx, slug, r["base"], r["siblings"])
    if r["discard_error"]:
        noun = scan.host_noun(scan.backend_for(sess), sess)
        _record(ctx, key, "engine_error", "engine-error",
                f"cleanup: removing the {noun} failed: {r['discard_error']}",
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
    """Deliver the code-changes requests the dashboard queued, on each run:
    under the state lock, so each goes out once."""
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
# `REQUEST <text>`, without the prefix), or an agent report queued as
# `REQUEST OCTOPILOT ...`. Bare report lines from before layer 4 are dropped.

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
    # Code text is verbatim: nothing appends a marker to it, so a trailing one
    # is part of what the user wrote.
    m = CODE.fullmatch(line)
    if m:
        return Request("code", (m.group(1),), False, False)
    line, confirmed, over_cap = _strip_confirmations(line)
    m = INVESTIGATE.fullmatch(line)
    if m:
        read_only, repo, question = m.groups()
        verb = "investigate-read-only" if read_only else "investigate"
        return Request(verb, (repo, question), confirmed, over_cap)
    words = line.split(maxsplit=1)
    if not words:
        return unknown
    verb, rest = words[0], (words[1] if len(words) > 1 else "")
    if verb in ONE_TARGET and len(rest.split()) == 1:
        return Request(verb, (rest,), confirmed, over_cap)
    if verb in NO_ARGS and not rest:
        return Request(verb, (), confirmed, over_cap)
    return unknown


def _key_number(key):
    """The number in owner/repo#N or owner/repo!N, else None."""
    m = re.search(r"[#!](\d+)$", key)
    return int(m.group(1)) if m else None


def _known_key(state, snap_rows, key):
    """The item key `key` names: a tracked item's, by its key or a re-keyed
    one, else `key` itself when a row has it, else None."""
    item_key, _ = locate_item(state, key)
    if item_key:
        return item_key
    if any(r["key"] == key for r in snap_rows):
        return key
    return None


def resolve_target(token, state, snap_rows):
    """Resolves a request target to an item key -> (key, error).

    Accepts a key or prev_key, repo#N or repo!N (either separator, repo a
    registry slug or a unique suffix of one), or a bare N naming one row.
    Pick-list numbers are the prompter's job, so they are not read here."""
    key = _known_key(state, snap_rows, token)
    if key:
        return key, None
    m = re.fullmatch(r"(.+?)([#!])(\d+)", token)
    if m:
        repo, sep, num = m.groups()
        slug, err = _repo_for(state, repo)
        if err:
            return None, err
        # Either separator names an item: prefer the one that is tracked or
        # proposed, else keep the one typed.
        built = f"{slug}{sep}{int(num)}"
        other = f"{slug}{'!' if sep == '#' else '#'}{int(num)}"
        return (_known_key(state, snap_rows, built)
                or _known_key(state, snap_rows, other) or built), None
    if token.isdigit():
        n = int(token)
        keys = sorted({r["key"] for r in snap_rows
                       if (r["kind"] == "PROPOSE" or r["kind"] in TRACKED)
                       and _key_number(r["key"]) == n})
        if not keys:
            return None, f"no proposed or tracked item #{n}"
        if len(keys) > 1:
            return None, f"#{n} is ambiguous: {' '.join(keys)}"
        return keys[0], None
    return None, f"unknown target {token!r}"


# ------------------------------------------------------------------- stacks

def _children(open_prs, pr):
    """The open PRs based on `pr`'s branch."""
    return [p for p in open_prs if p["baseRefName"] == pr["headRefName"]]


def stack_from(open_prs, num, base):
    """The stack PR `num` is in, as [{pr, branch}] bottom to top -> (layers, error).

    (None, None) is a plain PR: alone, or its bottom is not based on `base`.
    (None, error) names a fork or an unknown PR; no layer may have two children."""
    by_num = {p["number"]: p for p in open_prs}
    if num not in by_num:
        return None, f"#{num} is not an open PR"
    seen = {num}
    below = []
    cur = by_num[num]
    while cur["baseRefName"] != base:
        found = [p for p in open_prs if p["headRefName"] == cur["baseRefName"]]
        if not found:
            return None, None
        if len(found) > 1:
            return None, f"stack is ambiguous below #{cur['number']}"
        cur = found[0]
        if cur["number"] in seen:
            return None, f"stack loops at #{cur['number']}"
        seen.add(cur["number"])
        below.insert(0, cur)
    above = []
    top = by_num[num]
    while True:
        kids = _children(open_prs, top)
        if len(kids) != 1 or kids[0]["number"] in seen:
            break
        top = kids[0]
        seen.add(top["number"])
        above.append(top)
    chain = below + [by_num[num]] + above
    for p in chain:
        if len(_children(open_prs, p)) > 1:
            return None, f"stack forks at #{p['number']}"
    if len(chain) < 2:
        return None, None
    return [{"pr": p["number"], "branch": p["headRefName"]} for p in chain], None


def walk_stack(slug, num, base):
    """stack_from over the repo's open PRs, from one gh call."""
    import scan  # lazy: scan reads its state at import
    rows = scan.gh_json(["pr", "list", "-R", slug, "--state", "open",
                         "--limit", "200", "--json", scan.PR_FIELDS], default=None)
    if rows is None:
        return None, f"could not list the open PRs of {slug}"
    return stack_from(rows, num, base)


# ------------------------------------------------------------ starting work
#
# Request handlers: each returns its outcome line, or None for a report. A
# refusal or a hold is logged and consumed, never retried by the engine.

def _snapshot():
    """The last tick's snapshot, or {} before the first one. Read only: a request
    resolves against the rows the user is looking at."""
    import scan
    try:
        with open(scan.SNAPSHOT_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _refuse(ctx, verb, key, reason, **fields):
    """A request that cannot be done, logged with why."""
    kind = "go-refused" if verb == "go" else "request-refused"
    ctx.log(kind, key, verb=verb, reason=reason, **fields)
    return f"{verb} {key} refused: {reason}" if key else f"{verb} refused: {reason}"


def _hold(ctx, key, title, reason):
    """A start that the wind-down or the cap holds back: logged, not retried."""
    ctx.log("held", key, title=title, reason=reason)
    return f"{key or 'request'} held ({reason}): {title}"


def slots_used(state, snap):
    """The slots the cap counts now: the snapshot's active and untracked, and the
    items started after it."""
    # The snapshot cannot count what started after it: those items are added
    # later than its generated_at (the same second counts as later, to be safe).
    snap = snap or {}
    since = int(snap.get("generated_at") or 0)
    late = sum(1 for it in (state or {}).get("items", {}).values()
               if int(it.get("added") or 0) >= since)
    return int(snap.get("active") or 0) + int(snap.get("untracked") or 0) + late


def _held(req, state, snap):
    """Why a session may not start now -> "wind-down", "cap" or None. A request
    confirmed past wind-down passes that; one confirmed over the cap passes the cap."""
    import scan
    if scan.wind_down() == "yes" and not req.confirmed:
        return "wind-down"
    if slots_used(state, snap) >= scan.MAX_ACTIVE and not req.over_cap:
        return "cap"
    return None


def _tracked(state, key):
    """The key of the item holding `key`: itself, a key it was re-keyed from, or
    the stack a PR is a layer of. None when nothing does."""
    import scan
    found, _ = locate_item(state, key)
    if found:
        return found
    slug, num, kind = scan.split_key(key)
    return (scan.item_key_for(state, slug, num) or None) if kind == "pr" else None


def _row_title(rows, key):
    """The title the snapshot shows for `key`, else the key itself."""
    return next((r["title"] for r in rows if r["key"] == key and r.get("title")), key)


def _base(state, slug):
    return state.get("registry", {}).get(slug, {}).get("base", "main")


def _repo_for(state, token, hint=""):
    """The registry slug a request names: exact, or the one slug ending in
    /token -> (slug, error). `hint` follows the error when the repo is unknown."""
    slugs = list(state.get("registry", {}))
    if token in slugs:
        return token, None
    found = sorted(s for s in slugs if s.endswith("/" + token))
    if not found:
        return None, f"{token} is not in the registry{hint}"
    if len(found) > 1:
        return None, f"{token} is ambiguous: {' '.join(found)}"
    return found[0], None


def _behind(path, base, branch):
    """True when origin/<branch> does not contain origin/<base>: a rebase is due.
    Reads the refs the spawn just fetched."""
    import scan
    code, _, _ = scan.sh(["git", "-C", path, "merge-base", "--is-ancestor",
                          f"origin/{base}", f"origin/{branch}"])
    return code != 0


def do_go(req, ctx):
    """go <target>: work on an issue (a branch named by naming.choose_names), or on
    a PR. A PR's stack is one item, keyed by its bottom layer."""
    import scan
    state = scan.load()
    snap = _snapshot()
    rows = snap.get("rows", [])
    key, err = resolve_target(req.args[0], state, rows)
    if err:
        return _refuse(ctx, "go", None, err)
    title = _row_title(rows, key)
    slug, num, kind = scan.split_key(key)
    if kind == "investigate":
        return _refuse(ctx, "go", key, "an investigation starts with /investigate",
                       title=title)
    tracked = _tracked(state, key)
    if tracked:
        return _refuse(ctx, "go", key, f"already tracked as {tracked}", title=title)
    if slug not in state.get("registry", {}):
        return _refuse(ctx, "go", key, f"{slug} has no local clone under CLONE_ROOTS: "
                       "clone it first", title=title)
    held = _held(req, state, snap)
    if held:
        return _hold(ctx, key, title, held)

    # GitHub is read before the lock: the spawn holds it through its git fetch.
    base = _base(state, slug)
    layers, stack_spec, start = None, None, key
    if kind == "issue":
        iss = scan.gh_json(["issue", "view", str(num), "-R", slug, "--json",
                            "title,labels,state"], default=None)
        if not iss:
            return _refuse(ctx, "go", key, "could not read the issue", title=title)
        if iss.get("state") != "OPEN":
            return _refuse(ctx, "go", key, "issue is closed", title=title)
        branch, title = naming.choose_names("issue", number=num,
                                            title=iss.get("title") or "",
                                            labels=iss.get("labels") or [])
    else:
        d = scan.pr_detail(slug, num)
        if not d:
            return _refuse(ctx, "go", key, "could not read the PR", title=title)
        if d.get("state") != "OPEN":
            reason = "PR is merged" if d.get("state") == "MERGED" else "PR is closed"
            return _refuse(ctx, "go", key, reason, title=title)
        layers, err = walk_stack(slug, num, base)
        if err:
            return _refuse(ctx, "go", key, err, title=title)
        branch, title = d["headRefName"], (d.get("title") or "")[:60]
        if layers:
            start = scan.key(slug, layers[0]["pr"])
            branch = layers[0]["branch"]
            stack_spec = " ".join(f"{layer['pr']}:{layer['branch']}" for layer in layers)

    with scan.locked():
        state = scan.load()
        tracked = _tracked(state, start)
        if tracked:
            return _refuse(ctx, "go", start, f"already tracked as {tracked}", title=title)
        held = _held(req, state, snap)
        if held:
            return _hold(ctx, start, title, held)
        try:
            r = scan.spawn_item(state, start, branch, title, kind == "issue",
                                stack_spec=stack_spec)
        except (Exception, SystemExit) as exc:
            return _refuse(ctx, "go", start, f"spawn failed: {exc}", title=title)
        wt, path = _worktree(state["items"][start]), state["registry"][slug]["path"]
    # git runs with the lock released: a checkout or the behind check can take minutes.
    tmpl, subs = _first_prompt_for(r, start, branch, slug, num, layers, kind == "issue",
                                   path)
    mark, failure = None, None
    if tmpl in COMMENT_TEMPLATES:
        head, note = checkout_pr_head(wt, layers[0]["pr"] if layers else num, branch)
        if head:
            mark = {"op": "reviewed", "sha": head}
        else:
            failure = note
    # The prompt is rendered without the lock: its issue context is a gh call. A
    # render that fails here is left to _send_in, which queues it under its guard.
    try:
        rendered = scan.render_for(scan.load(), tmpl, dict(subs))
    except (Exception, SystemExit):
        rendered = None
    with scan.locked():
        return _send_first(scan.load(), ctx, start, r, title, tmpl, subs, mark, failure,
                           rendered)


# Templates that review someone else's PR on its head, and mark that head reviewed.
COMMENT_TEMPLATES = ("review-comment", "re-review", "review-stack")


def _first_prompt_for(r, start, branch, slug, num, layers, is_issue, path):
    """The template and substitutions of a fresh session's first prompt. A fix PR
    behind its base gets the rebase alone: it runs the review loop itself."""
    base, tmpl = r["base"], r["template"]
    if is_issue:
        return tmpl, {"REPO": slug, "ISSUE": num, "BRANCH": branch, "BASE": base}
    if r["stack"]:
        return tmpl, {"REPO": slug, "BASE": base, "KEY": start}
    subs = {"REPO": slug, "PR": num, "BRANCH": branch, "BASE": base}
    if r["prev"]:
        subs["PREV"] = r["prev"]
    if tmpl == "review-fix" and _behind(path, base, branch):
        tmpl = "rebase"
    return tmpl, subs


def _send_first(state, ctx, start, r, title, tmpl, subs, mark, failure, rendered=None):
    """Send the first prompt, or queue it when the host warned. A checkout that
    failed leaves the item booting: the executor's BOOTING row retries it. Caller
    holds the lock."""
    import scan
    k = scan.resolve_key(state, start)
    it = state.get("items", {}).get(k)
    if it is None:
        return _refuse(ctx, "go", start, "gone before its first prompt", title=title)
    if failure:
        it["phase"] = "booting"
        _note(state, ctx, k, "engine_error", "engine-error", f"go failed: {failure}",
              action="go")
        scan.save(state)
        return f"go {k} failed: {failure}; the next tick retries it"
    if r["warning"]:
        queue_send(state, k, tmpl, subs, "go", mark)
        scan.save(state)
        ctx.log("go", k, title=title, template=tmpl, queued=True, warning=r["warning"])
        return f"{k}: {tmpl} queued, the host warned: {r['warning']}"
    # A note (the host did something else) is logged; the prompt still goes out.
    fields = {"note": r["note"]} if r["note"] else {}
    ctx.log("go", k, title=title, template=tmpl, **fields)
    return _send_in(state, ctx, k, tmpl, subs, "go", mark, rendered)


def do_investigate(req, ctx):
    """/investigate [--read-only] <repo> <question>: a session for a question, not
    for a GitHub item. The question goes on the item verbatim; the name and
    branch are picked so no other investigation has them."""
    import scan
    repo, question = req.args
    state = scan.load()
    slug, err = _repo_for(state, repo, hint=": clone it under CLONE_ROOTS first")
    if err:
        return _refuse(ctx, req.verb, None, err)
    title = question.split("\n", 1)[0][:60]
    snap = _snapshot()
    held = _held(req, state, snap)
    if held:
        return _hold(ctx, None, title, held)
    read_only = req.verb == "investigate-read-only"
    with scan.locked():
        state = scan.load()
        held = _held(req, state, snap)
        if held:
            return _hold(ctx, None, title, held)
        name, branch, title = naming.choose_names(
            "investigation", question=question, read_only=read_only,
            repo_path=state["registry"][slug]["path"], repo_slug=slug,
            taken_keys=list(state.get("items", {})),
            taken_branches=[it.get("branch") for it in state.get("items", {}).values()])
        key = scan.key(slug, name, "investigate")
        try:
            r = scan.spawn_item(state, key, branch, title, True, question=question,
                                read_only=read_only)
        except (Exception, SystemExit) as exc:
            return _refuse(ctx, req.verb, key, f"spawn failed: {exc}", title=title)
        ctx.log("investigate", key, title=title, template=r["template"])
        return _send_in(state, ctx, key, r["template"],
                        {"KEY": key, "REPO": slug, "BASE": r["base"]}, "go")


def _target_of(req, ctx):
    """The item key a one-target request names -> (key, None), or (None, the
    refusal line) when the target is unknown or ambiguous. A stack layer names
    its stack's item."""
    import scan
    state = scan.load()
    key, err = resolve_target(req.args[0], state, _snapshot().get("rows", []))
    if err:
        return None, _refuse(ctx, req.verb, None, err)
    return _tracked(state, key) or key, None


def do_no(req, ctx):
    """no <target>: the item is declined and not proposed again."""
    import scan
    key, line = _target_of(req, ctx)
    if key is None:
        return line
    with scan.locked():
        scan.decline(scan.load(), [key])
    return f"{key}: declined"


def do_detach(req, ctx):
    """detach <target>: close the item's session, keep its worktree and branch,
    and untrack it so it is proposed again."""
    import scan
    key, line = _target_of(req, ctx)
    if key is None:
        return line
    with scan.locked():
        r = scan.detach_item(scan.load(), key)
    if not r["ok"]:
        return _refuse(ctx, req.verb, key, r["error"])
    return r["msg"]


def do_ack(req, ctx):
    """ack <target>: the user has seen the PUSHED flag; stop flagging this head."""
    import scan
    key, line = _target_of(req, ctx)
    if key is None:
        return line
    with scan.locked():
        r = scan.ack_push(scan.load(), key)
    if not r["ok"]:
        return _refuse(ctx, req.verb, key, r["error"])
    return f"{key}: acked"


def do_pause(req, ctx):
    """pause / resume: the engine's work stops or starts; see drain for what waits."""
    import scan
    paused = req.verb == "pause"
    with scan.locked():
        scan.set_paused(scan.load(), paused)
    return "paused" if paused else "resumed"


def do_re_review(req, ctx):
    """re-review <target>: a follow-up review of a comment PR's new commits, through
    the executor's review path, so the review cap and the reboot apply."""
    import scan
    key, line = _target_of(req, ctx)
    if key is None:
        return line
    it = find_item(scan.load(), key)
    if it is None:
        return _refuse(ctx, req.verb, key, "not tracked")
    if it.get("mode") != "comment":
        return _refuse(ctx, req.verb, key, "only a comment PR gets a re-review")
    rounds = int(it.get("review_rounds", 0))
    if rounds >= scan.MAX_REVIEW_ROUNDS:
        return _refuse(ctx, req.verb, key,
                       f"at the review cap ({rounds}/{scan.MAX_REVIEW_ROUNDS})")
    return execute(Action("review", key, {}), ctx)


def do_code(req, ctx):
    """/octopilot-code <text>: a request for the code-changes agent, verbatim. It
    holds no slot and is never tracked."""
    import scan
    with scan.locked():
        sent, err = scan.send_code(scan.load(), req.args[0])
    if err:
        return f"{scan.CODE_AGENT}: kept, still pending ({err})"
    return f"{scan.CODE_AGENT}: {sent} request(s) delivered"


def do_tick(req, ctx):
    """tick: a full tick was asked for. A drain runs no scan of its own: the
    dashboard's GitHub loop is kicked by the event, and that tick acts."""
    ctx.log("tick-asked", None)
    return "tick: the next tick acts, asked soon"


def _unknown(req, ctx):
    """A request outside the grammar: logged and refused, never guessed at."""
    ctx.log("request-unknown", None, text=req.args[0][:200])
    return f"unknown request: {req.args[0][:120]}"


HANDLERS = {"go": do_go, "no": do_no, "detach": do_detach, "ack": do_ack,
            "pause": do_pause, "resume": do_pause, "re-review": do_re_review,
            "investigate": do_investigate, "investigate-read-only": do_investigate,
            "code": do_code, "tick": do_tick}


def _handle(req, ctx):
    """One request -> its outcome line, or None when it has nothing to say. A
    report only gets here as a `REQUEST OCTOPILOT ...` line; it is dropped, since
    the agent's report is already recorded on its item."""
    if req.verb == "report":
        return None
    if req.verb == "unknown":
        return _unknown(req, ctx)
    return HANDLERS[req.verb](req, ctx)


# ------------------------------------------------------------------- drain
#
# The inbox takes one message at a time under the state lock, so a request
# that arrives mid-drain waits for its turn rather than being overwritten.

# Requests that wait in the inbox while paused (Q1): the rest run at once.
HELD_WHILE_PAUSED = {"go", "investigate", "investigate-read-only", "re-review"}
# _next_request's answer when the budget is spent and a request is waiting.
SPENT = Request("spent", (), False, False)


def _request_text(message):
    """A REQUEST inbox message as the request it carries: the prefix goes."""
    return message[len("REQUEST "):]


def _next_request(ctx):
    """Pop the first inbox message this drain handles -> its Request. None when
    none is left to handle, SPENT when the budget is gone with one left. Held
    messages stay where they are, so the rest keep their order. Bare agent
    reports (queued before layer 4) are dropped: the report is on its item."""
    import scan
    with scan.locked():
        state = scan.load()
        box = state.get("inbox") or []
        requests = [m for m in box if m["text"].startswith("REQUEST ")]
        if len(requests) != len(box):
            state["inbox"] = box = requests
            scan.save(state)
        for i, m in enumerate(box):
            req = parse_request(_request_text(m["text"]))
            # Read per message: a resume earlier in this drain releases the held ones.
            if state.get("paused") and req.verb in HELD_WHILE_PAUSED:
                continue
            if _over_budget(ctx):
                return SPENT
            state["inbox"] = box[:i] + box[i + 1:]
            scan.save(state)
            return req
    return None


def _run_inbox(ctx):
    """Handle the inbox's requests in order -> their outcome lines."""
    lines = []
    while True:
        req = _next_request(ctx)
        if req is None:
            return lines
        if req is SPENT:
            lines.append("budget spent: the inbox waits for the next drain")
            return lines
        try:
            line = _handle(req, ctx)
        except (Exception, SystemExit) as exc:
            # The request is consumed; one bad request must not end the drain.
            ctx.log("request-failed", None, verb=req.verb,
                    error=f"{type(exc).__name__}: {exc}"[:200])
            line = f"{req.verb} failed: {type(exc).__name__}: {exc}"
        if line:
            lines.append(line)


def _verdict(req, state, snap):
    """What a go or an investigation would do, from the state and the snapshot."""
    if req.verb == "go":
        key, err = resolve_target(req.args[0], state, snap.get("rows", []))
        if err:
            return f"refuse ({err})"
        tracked = _tracked(state, key)
        if tracked:
            return f"refuse (already tracked as {tracked})"
    held = _held(req, state, snap)
    return f"hold ({held})" if held else "start"


def _plan_inbox(ctx):
    """What a drain would do with each inbox message, changing nothing."""
    import scan
    state = scan.load()
    snap = _snapshot()
    lines = []
    for m in state.get("inbox") or []:
        if not m["text"].startswith("REQUEST "):
            first = (m["text"].strip().splitlines() or [""])[0][:120]
            lines.append(f"would drop (agent report, already on its item): {first}")
            continue
        text = _request_text(m["text"])
        req = parse_request(text)
        first = (text.strip().splitlines() or [""])[0][:120]
        if state.get("paused") and req.verb in HELD_WHILE_PAUSED:
            lines.append(f"held while paused: {first}")
        elif req.verb in ("go", "investigate", "investigate-read-only"):
            lines.append(f"would {_verdict(req, state, snap)}: {first}")
        else:
            lines.append(f"would handle: {first}")
    return lines


def drain(mode, started, clock=time.monotonic):
    """Handle the inbox's requests in order, then the queued sends -> the outcome
    lines. Plan mode changes nothing. Caller holds exec.lock, not the state lock."""
    import scan
    ctx = Ctx(dry_run=mode == "plan", sent_keys=set(), log=scan.log_event,
              rows=_snapshot().get("rows", []),
              deadline=started + ACT_BUDGET_SECS, clock=clock)
    lines = _plan_inbox(ctx) if ctx.dry_run else _run_inbox(ctx)
    return lines + deliver_pending(ctx)
