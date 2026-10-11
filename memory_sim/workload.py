"""Fabricated events and an independent grading ledger."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import random
import re
import sqlite3
from pathlib import Path

MARKER = re.compile(
    r"(?P<action>FACT|DELETE)\[(?P<key>f-\d+)\]"
    r"(?:=(?P<value>v-[a-z0-9-]+))?\s+REV\[(?P<revision>\d+)\]"
)


@dataclass(frozen=True)
class Event:
    turn: int
    key: str
    value: str | None
    group: str
    critical: bool
    user: str
    assistant: str
    messages: list[dict]


def observed_state(text: str) -> dict[str, tuple[int, str | None]]:
    """Read source markers only. Never consult expected values."""
    state = {}
    for match in MARKER.finditer(text):
        key, revision = match["key"], int(match["revision"])
        if match["action"] == "FACT" and not match["value"]:
            continue
        value = match["value"] if match["action"] == "FACT" else None
        if key not in state or revision >= state[key][0]:
            state[key] = (revision, value)
    return state


def render_state(state: dict[str, tuple[int, str | None]]) -> str:
    lines = []
    for key, (revision, value) in sorted(state.items()):
        if value is None:
            lines.append(f"DELETE[{key}] REV[{revision}]")
        else:
            lines.append(f"FACT[{key}]={value} REV[{revision}]")
    return "\n".join(lines)


WORKLOAD_VERSION = 2


def critical_numbers(seed, facts):
    """Stratify critical facts across short, buried and tool-only evidence."""
    rng = random.Random(seed ^ 0xC17CA1)
    groups = {}
    kinds = ("short", "buried", "tool", "short", "buried")
    for number in range(facts):
        groups.setdefault(kinds[number % len(kinds)], []).append(number)
    for members in groups.values():
        rng.shuffle(members)
    selected = set()
    count = max(len(groups), (facts + 9) // 10)
    while groups and len(selected) < count:
        for group in sorted(list(groups)):
            selected.add(groups[group].pop())
            if not groups[group]:
                del groups[group]
            if len(selected) == count:
                break
    return selected


def iter_generate(seed: int, facts: int, turns: int, payload_chars: int,
                  shape: dict | None = None):
    rng = random.Random(seed)
    groups = ("short", "buried", "tool", "short", "buried")
    critical = critical_numbers(seed, facts)
    lengths = (shape or {}).get("length_samples", [])
    for turn in range(turns):
        number = turn if turn < facts else rng.randrange(facts)
        key = f"f-{number:05d}"
        group = groups[number % len(groups)]
        # Never delete initial introductions; later revisions may be tombstones.
        value = None if turn >= facts and rng.random() < .12 else f"v-{rng.getrandbits(40):010x}"
        revision = turn + 1
        marker = (f"DELETE[{key}] REV[{revision}]" if value is None else
                  f"FACT[{key}]={value} REV[{revision}]")
        length = min(8192, max(96, rng.choice(lengths))) if lengths else payload_chars
        padding = ("Synthetic operational background. " * (length // 34 + 1))[:length]
        body = f"{padding}\n{marker}" if group == "buried" else f"{marker}\n{padding}"
        if group == "tool":
            user = f"Inspect synthetic setting {key}. {padding[:120]}"
            assistant = "Inspected the synthetic setting; evidence is in the tool result."
            call_id = f"synthetic-{seed}-{turn}"
            messages = [
                {"role": "user", "content": user},
                {"role": "assistant", "content": "", "tool_calls": [{
                    "id": call_id, "type": "function",
                    "function": {"name": "synthetic_inspect", "arguments": json.dumps({"key": key})},
                }]},
                {"role": "tool", "content": body, "tool_call_id": call_id,
                 "tool_name": "synthetic_inspect"},
                {"role": "assistant", "content": assistant},
            ]
        else:
            user, assistant = body, "Recorded the synthetic setting."
            messages = [{"role": "user", "content": user},
                        {"role": "assistant", "content": assistant}]
        yield Event(turn, key, value, group, number in critical, user, assistant, messages)


def generate(seed: int, facts: int, turns: int, payload_chars: int,
             shape: dict | None = None) -> list[Event]:
    return list(iter_generate(seed, facts, turns, payload_chars, shape))


class Ledger:
    """Only the grader owns this object; adapters receive prompts, not this ledger."""
    def __init__(self):
        self.latest: dict[str, Event] = {}
        self.known: dict[str, set[str]] = {}

    def apply(self, event: Event):
        self.latest[event.key] = event
        if event.value is not None:
            self.known.setdefault(event.key, set()).add(event.value)

    def grade_state(self, source: dict[str, tuple[int, str | None]]) -> dict:
        correct = critical_correct = tombstones_saved = 0
        critical_total = answerable = 0
        for key, event in self.latest.items():
            present = key in source and source[key] == (event.turn + 1, event.value)
            correct += present
            if event.value is None:
                tombstones_saved += present
            else:
                answerable += 1
            if event.critical:
                critical_total += 1
                critical_correct += present
        return {"correct": correct, "total": len(self.latest),
                "critical_correct": critical_correct, "critical_total": critical_total,
                "answerable": answerable, "tombstones_saved": tombstones_saved}


def workload_hash(events: list[Event]) -> str:
    blob = json.dumps([asdict(event) for event in events], sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def profile_shape(profile: Path) -> dict:
    """Numeric message shapes only; no conversation text enters the generated corpus."""
    db = sqlite3.connect(f"file:{profile / 'state.db'}?mode=ro", uri=True)
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    rows = db.execute(
        "SELECT role,length(content) FROM messages WHERE content IS NOT NULL ORDER BY id DESC LIMIT 10000"
    ).fetchall()
    db.close()
    lengths = [length for _, length in rows if length]
    counts = {}
    for role, _ in rows:
        counts[role] = counts.get(role, 0) + 1
    ordered = sorted(lengths)
    return {
        "source": "Local read-only SQLite length/role metadata; no private text copied.",
        "sample_count": len(rows), "role_counts": counts,
        "length_samples": lengths[:1000],
        "p50_chars": ordered[len(ordered) // 2] if ordered else 0,
        "p95_chars": ordered[min(len(ordered) - 1, int(len(ordered) * .95))] if ordered else 0,
    }
