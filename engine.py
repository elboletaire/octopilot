"""Rules for which snapshot rows are still worth acting on.

A row is computed from the state at one scan. Actions taken after it (a
cleanup, a feedback mark, a decline) can make it stale, so the executor asks
`revalidate_row` before acting on each row. revalidate_row is pure (no I/O, no
imports from scan); deliver_pending does its I/O through scan.

Rows must come from the tick's saved scan, not from snapshot()'s copy: a
forward re-key (issue key -> PR key) is not mapped by prev_keys, so an issue
row cannot be followed to the PR it became.
"""
import time
from collections import namedtuple
from dataclasses import dataclass
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


@dataclass
class Ctx:
    """One run of the executor. `sent_keys` holds the items already sent to
    in this run, so each item gets at most one prompt per run."""
    dry_run: bool
    sent_keys: set
    log: Callable
    rows: list


def _same_item(state, a, b):
    """Whether two keys name the same tracked item, one maybe a prev key."""
    if a == b:
        return True
    item = find_item(state, a)
    return item is not None and item is find_item(state, b)


def queue_send(state, key, template, subs, reason, mark=None):
    """Queue a prompt for `key` to be rendered and sent at delivery. A newer
    one for the same item replaces the older one, whatever key it was queued
    under. `mark` is the state change the send stands for (see _apply_mark),
    made only once the prompt has gone out."""
    sends = [p for p in state.get("pending_sends", [])
             if not _same_item(state, p["key"], key)]
    entry = {"key": key, "template": template, "subs": subs,
             "at": int(time.time()), "tries": 0, "error": None,
             "reason": reason}
    if mark:
        entry["mark"] = mark
    sends.append(entry)
    state["pending_sends"] = sends


def deliver_pending(ctx):
    """Try each queued send once. Returns one outcome line per entry tried.

    Each entry takes the state lock on its own, so this must not run while
    the caller holds it: the lock is not re-entrant.
    """
    # Imported late: scan will import engine for the tick.
    import scan
    out = []
    keys = [p["key"] for p in scan.load().get("pending_sends", [])]
    for key in keys:
        with scan.locked():
            state = scan.load()
            entry = next((p for p in state.get("pending_sends", [])
                          if p["key"] == key), None)
            if entry is None:
                continue
            # The item may have been re-keyed since queueing: sent_keys and the
            # executor both use the item's current key.
            resolved = scan.resolve_key(state, key)
            if resolved in ctx.sent_keys:
                continue
            # send_item exits on an unknown item, so check before sending.
            if resolved not in state.get("items", {}):
                if ctx.dry_run:
                    out.append(f"{key}: would drop, item gone")
                    continue
                state["pending_sends"] = [p for p in state["pending_sends"]
                                          if p["key"] != key]
                scan.save(state)
                ctx.log("send-dropped", key, template=entry["template"])
                out.append(f"{key}: dropped, item gone")
                continue
            ctx.sent_keys.add(resolved)
            if ctx.dry_run:
                out.append(f"{key}: would send {entry['template']}")
                continue
            # A crash here must not wedge the queue: the entry counts as a
            # failed try and the next entry still goes out.
            try:
                text = scan.render_for(state, entry["template"], entry["subs"])
                code = scan.send_item(state, resolved, text)
            except (Exception, SystemExit) as exc:
                entry["tries"] += 1
                entry["error"] = f"{FAILED}: {type(exc).__name__}: {exc}"
                out.append(f"{key}: failed, try {entry['tries']}")
                scan.save(state)
                continue
            if code == 0:
                if entry.get("mark"):
                    _apply_mark(state, resolved, entry["mark"])
                state["pending_sends"] = [p for p in state["pending_sends"]
                                          if p["key"] != key]
                out.append(f"{key}: sent")
            elif code == 4:
                if entry["error"] != BLOCKED:
                    ctx.log("send-blocked", key, template=entry["template"])
                entry["error"] = BLOCKED
                out.append(f"{key}: blocked, kept for retry")
            else:
                entry["tries"] += 1
                entry["error"] = FAILED
                out.append(f"{key}: failed, try {entry['tries']}")
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


def _send_in(state, ctx, key, template, subs, reason, mark=None):
    """Send `template` to the item under `key`, or queue it when its agent
    cannot take it now (exit 4, or a failed send). Caller holds the lock and
    passes the state it loaded."""
    import scan
    k = scan.resolve_key(state, key)
    if k not in state.get("items", {}):
        return f"{key}: not tracked, nothing sent"
    if k in ctx.sent_keys:
        return f"{k}: already sent this run"
    ctx.sent_keys.add(k)
    text = scan.render_for(state, template, dict(subs))
    if scan.send_item(state, k, text) != 0:
        queue_send(state, key, template, subs, reason, mark)
        scan.save(state)
        ctx.log("send-queued", k, template=template, reason=reason)
        return f"{k}: queued {template}, kept for retry"
    # A fresh prompt supersedes any queued one for the item, so that one must
    # not go out later as a stale duplicate.
    state["pending_sends"] = [p for p in state.get("pending_sends", [])
                              if not _same_item(state, p["key"], k)]
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
    return [_send(ctx, k, *_rebase_prompt(slug, base, k, it), "rebase")
            for k, it in siblings]


def checkout_pr_head(wt, pr, branch):
    """Detach the worktree `wt` on the head of PR `pr`. -> (sha, note): sha is
    the head it now sits on, or None with the reason in note. The worktree is
    never moved over uncommitted changes."""
    import scan
    if not wt:
        return None, "no worktree recorded"
    code, _, err = scan.sh(["git", "-C", wt, "fetch", "origin", f"pull/{pr}/head"],
                           timeout=180)
    if code != 0:
        # The PR's own ref can be missing (a fork); its branch may still be there.
        code, _, err = scan.sh(["git", "-C", wt, "fetch", "origin", branch],
                               timeout=180)
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


def _cleanup(ctx, key):
    import scan
    with scan.locked():
        state = scan.load()
        r = scan.cleanup_item(state, scan.resolve_key(state, key))
    if not r["ok"]:
        ctx.log("engine-error", key, action="cleanup", error=r["error"])
        return f"{key}: cleanup refused: {r['error']}"
    if r["discard_error"]:
        ctx.log("engine-error", key, action="cleanup", error=r["discard_error"])
    slug = scan.split_key(key)[0]
    sent = _fan_out(ctx, slug, r["base"], r["siblings"])
    return "; ".join([f"{key}: cleaned up"] + sent)


def _restack(ctx, key):
    import scan
    with scan.locked():
        r = scan.restack_item(scan.load(), key)
    if not r["ok"]:
        ctx.log("engine-error", key, action="restack", error=r["error"])
        return f"{key}: restack refused: {r['error']}"
    slug = scan.split_key(key)[0]
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
        code = scan.reboot(state, k)
    if code == 0:
        return _boot(ctx, k)
    if code == 3:
        return f"{k}: rebooting, not idle yet; booting next tick"
    ctx.log("engine-error", k, action="review", error=f"reboot exit {code}")
    return f"{k}: reboot failed (exit {code})"


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
        ctx.log("engine-error", k, action="boot", error="PR detail unavailable")
        return f"{k}: boot not sent: PR detail unavailable"
    template, prev = scan.boot_template(it, d, me)
    head, note = checkout_pr_head(it.get("worktree"), pr, branch)
    if head is None:
        # Not sent: a review of a worktree that is not on the PR's head would
        # be a review of the wrong code. The row is still BOOTING next tick.
        ctx.log("engine-error", k, action="boot", error=note)
        return f"{k}: boot not sent: {note}"
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
    logged as engine-error and returned, never raised, so the next one still
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
        ctx.log("engine-error", action.key, action=action.kind,
                error=f"{type(exc).__name__}: {exc}")
        return f"{action.key}: {action.kind} failed: {exc}"


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


def run_rows(rows, mode):
    """Plan the tick's `rows` (scan's (kind, key, extra) tuples) and, in on
    mode, act on the plan. Plan mode logs what would happen and changes
    nothing. Caller holds exec.lock, not the state lock. -> the outcome lines."""
    import scan
    state = scan.load()
    dict_rows = [{"kind": kind, "key": key,
                  "fields": scan.parse_extra(extra)["fields"]}
                 for kind, key, extra in sorted(rows, key=scan.row_order)]
    dry = mode == "plan"
    ctx = Ctx(dry_run=dry, sent_keys=set(), log=scan.log_event, rows=dict_rows)
    lines = []
    for action in plan_rows(dict_rows, state):
        if dry:
            scan.log_event("engine-plan", action.key, action=action.kind)
        lines.append(execute(action, ctx))
    lines += deliver_pending(ctx)
    if not dry:
        lines += _deliver_code()
    return lines
