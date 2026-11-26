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


def queue_send(state, key, template, subs, reason):
    """Queue a prompt for `key` to be rendered and sent at delivery. A newer
    one for the same item replaces the older one, whatever key it was queued
    under."""
    sends = [p for p in state.get("pending_sends", [])
             if not _same_item(state, p["key"], key)]
    sends.append({"key": key, "template": template, "subs": subs,
                  "at": int(time.time()), "tries": 0, "error": None,
                  "reason": reason})
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
