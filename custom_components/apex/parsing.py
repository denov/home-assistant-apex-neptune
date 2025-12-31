# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import re
from typing import Any

_RE_UNDERSCORE_NUM = re.compile(r"^(?P<base>[A-Za-z]+)_(?P<num>\d+)$")                 # Amp_14
_RE_TRAILING = re.compile(r"^(?P<base>[A-Za-z]+)(?:(?P<module>x\d+)|(?P<num>\d+))$")  # Tmp2, Alkx5, pHx8


def split_probe_name(name: str) -> tuple[str, str | None, str | None]:
    m = _RE_UNDERSCORE_NUM.match(name)
    if m:
        return m.group("base"), m.group("num"), None

    m = _RE_TRAILING.match(name)
    if m:
        return m.group("base"), m.group("num"), m.group("module")

    return name, None, None


def pretty_type(probe_name: str, probe_type: str) -> str:
    t = (probe_type or "").strip().lower()
    n = (probe_name or "").strip().lower()

    if t == "temp":
        return "Temp"
    if t == "ph":
        return "pH"
    if t == "amps":
        return "Current"
    if t == "cond":
        return "Salinity" if n.startswith("salt") else "Conductivity"
    if t == "alk":
        return "Alkalinity"
    if t == "ca":
        return "Calcium"
    if t == "mg":
        return "Magnesium"

    return probe_type.strip() if probe_type else probe_name


def build_labels(
    probes: dict[str, dict[str, Any]],
    overrides: dict[str, tuple[str, str | None]],
) -> dict[str, str]:
    base_for: dict[str, str] = {}
    suffix_for: dict[str, str] = {}

    for probe_name, p in probes.items():
        if overrides and probe_name in overrides:
            base_for[probe_name] = overrides[probe_name][0]
            suffix_for[probe_name] = ""
            continue

        typ = (p.get("type") or "").strip()
        base = pretty_type(probe_name, typ)

        _, num, module = split_probe_name(probe_name)
        if typ.strip().lower() == "amps":
            suffix = num or module or ""
        else:
            suffix = module or num or ""

        base_for[probe_name] = base
        suffix_for[probe_name] = suffix

    counts: dict[str, int] = {}
    for base in base_for.values():
        counts[base] = counts.get(base, 0) + 1

    labels: dict[str, str] = {}
    for probe_name, base in base_for.items():
        if overrides and probe_name in overrides:
            labels[probe_name] = overrides[probe_name][0]
            continue

        typ = (probes.get(probe_name, {}).get("type") or "").strip().lower()
        suffix = suffix_for.get(probe_name) or ""

        if typ == "amps":
            labels[probe_name] = f"{base} {suffix}".strip() if suffix else base
        elif counts.get(base, 0) > 1 and suffix:
            labels[probe_name] = f"{base} ({suffix})" if suffix.startswith("x") else f"{base} {suffix}"
        else:
            labels[probe_name] = base

    return labels


def one_apex_prefix(name: str) -> str:
    raw = (name or "").strip()
    if raw.lower().startswith("apex "):
        raw = raw[5:].lstrip()
    return f"Apex {raw}"


def safe_uid_base(s: str) -> str:
    # keep stable unique_ids even if serial has ":" etc.
    return re.sub(r"[^a-z0-9_]+", "_", (s or "").lower()).strip("_")
