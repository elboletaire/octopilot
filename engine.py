"""Rules for which snapshot rows are still worth acting on.

A row is computed from the state at one scan. Actions taken after it (a
cleanup, a feedback mark, a decline) can make it stale, so the executor asks
`revalidate_row` before acting on each row. Pure: no I/O, no imports from scan.

Rows must come from the tick's saved scan, not from snapshot()'s copy: a
forward re-key (issue key -> PR key) is not mapped by prev_keys, so an issue
row cannot be followed to the PR it became.
"""

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
