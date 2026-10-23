from __future__ import annotations

import hashlib
import json
from typing import Any


GENESIS_HASH = "0" * 64


def canonical_event(event: dict[str, Any]) -> str:
    payload = {key: value for key, value in event.items() if key != "entry_hash"}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def chain_events(existing: list[dict], new: list[dict]) -> list[dict]:
    """LangGraph reducer that stamps events with a sequence number and hash chain."""
    chained = list(existing or [])
    for event in new or []:
        if "seq" in event and event.get("entry_hash"):
            chained.append(dict(event))
            continue
        item = {key: value for key, value in event.items() if key != "entry_hash"}
        item["seq"] = len(chained) + 1
        item["prev_hash"] = chained[-1].get("entry_hash", GENESIS_HASH) if chained else GENESIS_HASH
        item["entry_hash"] = hashlib.sha256(
            (item["prev_hash"] + canonical_event(item)).encode("utf-8")
        ).hexdigest()
        chained.append(item)
    return chained


def verify_audit_chain(events: list[dict] | None) -> dict[str, Any]:
    events = list(events or [])
    if not events:
        return {
            "valid": True,
            "events_checked": 0,
            "root_hash": GENESIS_HASH,
            "broken_at": None,
            "reason": "",
        }
    previous = GENESIS_HASH
    for index, event in enumerate(events, start=1):
        if event.get("seq") != index:
            return {
                "valid": False,
                "events_checked": len(events),
                "root_hash": previous,
                "broken_at": index,
                "reason": f"事件序号不连续：期望 {index}，实际 {event.get('seq')}",
            }
        if event.get("prev_hash") != previous:
            return {
                "valid": False,
                "events_checked": len(events),
                "root_hash": previous,
                "broken_at": index,
                "reason": "事件 prev_hash 与上一条 entry_hash 不一致",
            }
        expected = hashlib.sha256(
            (previous + canonical_event(event)).encode("utf-8")
        ).hexdigest()
        if event.get("entry_hash") != expected:
            return {
                "valid": False,
                "events_checked": len(events),
                "root_hash": previous,
                "broken_at": index,
                "reason": "事件内容与 entry_hash 不一致，可能被篡改",
            }
        previous = event["entry_hash"]
    return {
        "valid": True,
        "events_checked": len(events),
        "root_hash": previous,
        "broken_at": None,
        "reason": "",
    }
