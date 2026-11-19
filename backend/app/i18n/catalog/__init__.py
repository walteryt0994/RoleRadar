"""The interface messages, by area: one key per message, (简体中文, English) side by side.

An English value may be a pair (one, other) for a message that changes with a count (app.i18n.tn). Keys are stable
names, never the words themselves; the words are never used as codes, request parameters or stored values. Two areas
naming the same key is a mistake caught when this package is imported.
"""
from app.i18n.catalog import analysis, common, data, directions, jobs, profile

_AREAS = (common, profile, directions, jobs, analysis, data)


def _merge() -> dict[str, tuple]:
    merged: dict[str, tuple] = {}

    for area in _AREAS:
        for key, entry in area.MESSAGES.items():
            if key in merged:
                raise RuntimeError(f"interface message {key} is defined twice")

            merged[key] = entry

    return merged


MESSAGES = _merge()
