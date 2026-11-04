"""Per-file detection, conservative aliases, and stable labels; no global mappings."""

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .contract import MAX_CHARACTERS, JobError

TYPE_LABELS = {
    "PERSON": "Person",
    "LOCATION": "Address",
    "EMAIL_ADDRESS": "Email",
    "PHONE_NUMBER": "Phone",
}


@dataclass
class Unit:
    """Exact analyzer text plus handler-specific structural location and geometry."""

    text: str
    location: dict
    geometry: list | None = None
    detections: list = field(default_factory=list)


def detect(units, analyze):
    """Analyze bounded units without silently splitting a possible entity at a boundary."""
    import ase_document_tools as tools

    if sum(len(unit.text) for unit in units) > MAX_CHARACTERS:
        raise JobError("document_character_limit")
    for unit in units:
        if len(unit.text) > 100_000:
            raise JobError("analysis_block_limit")
        try:
            raw = tools._detections(analyze(unit.text), unit.text) if unit.text else []
        except RuntimeError:
            raise JobError("analyzer_failed", True) from None
        # A connected overlap component is one replacement, preventing nested PII leakage.
        merged = []
        for item in sorted(raw, key=lambda d: (d["start"], -d["end"], -d["score"])):
            item = dict(item)
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", item["entity_type"]):
                raise JobError("invalid_entity_type")
            if merged and item["start"] < merged[-1]["end"]:
                prior = merged[-1]
                prior["end"] = max(prior["end"], item["end"])
                if prior["entity_type"] != item["entity_type"]:
                    prior["entity_type"] = "PII"
                prior["score"] = max(prior["score"], item["score"])
            else:
                merged.append(item)
        unit.detections = merged


class Labels:
    """Allocate labels in extraction order, joining only explicit parenthetical aliases.

    :param units: Detected document units in stable file order.
    """

    def __init__(self, units):
        keys = []
        for unit in units:
            for d in unit.detections:
                keys.append((d["entity_type"], unit.text[d["start"] : d["end"]]))
        aliases = {}
        candidates = defaultdict(set)
        for unit in units:
            persons = [d for d in unit.detections if d["entity_type"] == "PERSON"]
            for full, short in zip(persons, persons[1:]):
                name = unit.text[full["start"] : full["end"]]
                alias = unit.text[short["start"] : short["end"]]
                if (
                    len(name.split()) >= 2
                    and alias in name.split()
                    and re.fullmatch(r"\s*\(\s*", unit.text[full["end"] : short["start"]])
                    and re.match(r"\s*\)", unit.text[short["end"] :])
                ):
                    candidates[alias].add(name)
        for short, names in candidates.items():
            if len(names) == 1:
                aliases[("PERSON", short)] = ("PERSON", next(iter(names)))
        people = {value for kind, value in keys if kind == "PERSON"}
        signatures = defaultdict(set)
        name_parts = defaultdict(set)
        for value in people:
            parts = value.casefold().split()
            if len(parts) >= 2:
                signatures[(parts[0][0], parts[-1])].add(value)
                for part in value.split():
                    name_parts[part].add(value)
        uncertain = (
            any((len(names) != 1) for names in candidates.values())
            or any(
                len(short.split()) == 1
                and ("PERSON", short) not in aliases
                and bool(name_parts.get(short))
                for short in people
            )
            or any(len(names) > 1 for names in signatures.values())
        )
        self.warnings = ["uncertain_aliases"] if uncertain else []
        self.values = {}
        counts = Counter()
        for key in keys:
            canonical = aliases.get(key, key)
            if canonical not in self.values:
                counts[key[0]] += 1
                prefix = TYPE_LABELS.get(key[0], "Entity")
                # Unknown entity types need separate visible prefixes to avoid label collisions.
                if key[0] not in TYPE_LABELS:
                    prefix = "Entity " + re.sub(r"[^A-Z0-9_]", "", key[0])[:64]
                self.values[canonical] = f"{prefix} {counts[key[0]]}"
            self.values[key] = self.values[canonical]

    def replacement(self, unit, detection):
        """Return a label without exposing source values outside this file context."""
        return self.values[
            (detection["entity_type"], unit.text[detection["start"] : detection["end"]])
        ]


def replace(unit, mode, labels):
    """Replace original code-point spans, keeping untouched text and line endings."""
    result = unit.text
    for detection in reversed(unit.detections):
        value = labels.replacement(unit, detection) if mode == "sanitize" else "[REDACTED]"
        result = result[: detection["start"]] + value + result[detection["end"] :]
    return result


def annotations(units):
    """Return structural locations and scores, never quoted values or identity mappings."""
    return [{"location": unit.location, **d} for unit in units for d in unit.detections]
