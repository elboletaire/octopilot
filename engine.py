"""Rules for which snapshot rows are still worth acting on.

A row is computed from the state at one scan. Actions taken after it (a
cleanup, a feedback mark, a decline) can make it stale, so the executor asks
`revalidate_row` before acting on each row. Pure: no I/O, no imports from scan.
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


def revalidate_row(row, state):
    """True when the snapshot `row` still holds against `state`."""
    kind, key = row["kind"], row["key"]
    fields = row.get("fields", {})
    it = find_item(state, key)
    if kind in TRACKED and it is None:
        return False
    if kind == "MERGED":
        return key not in state.get("handled_merges", [])
    if kind == "RESTACK":
        return bool(it.get("stack"))
    if kind == "REVIEW":
        return it.get("phase") != "booting"
    if kind == "BOOTING":
        return it.get("phase") == "booting"
    if kind == "FEEDBACK":
        # Same watermark as scan.layer_since: a layer's own, else the item's.
        pr = fields.get("pr")
        seen = (it.get("layer_seen") or {}).get(str(pr)) if pr else None
        seen = seen or it.get("feedback_seen")
        return fields.get("since", "") > (seen or "")
    if kind == "PROPOSE":
        return it is None and key not in state.get("declined", [])
    return True
