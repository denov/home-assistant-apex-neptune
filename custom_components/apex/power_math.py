# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from typing import Any


def total_current_amps(probes: dict[str, dict[str, Any]] | None) -> float | None:
    """Sum all probes of type 'Amps'. Returns None if none found."""
    probes = probes or {}
    total = 0.0
    found = False

    for p in probes.values():
        if (p.get("type") or "").strip().lower() == "amps":
            v = p.get("value")
            if isinstance(v, (int, float)):
                total += float(v)
                found = True

    return total if found else None


def total_power_watts(probes: dict[str, dict[str, Any]] | None, line_voltage: float) -> float | None:
    """Compute total watts = total_amps * line_voltage. Returns None if no amps probes found."""
    amps = total_current_amps(probes)
    if amps is None:
        return None
    return amps * float(line_voltage)
