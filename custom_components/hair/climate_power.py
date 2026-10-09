"""Opt-in matrix power policy; protocol bytes stay in the profile.

Each settings cell carries five complete, single-frame transmit recipes.
Unknown power uses a timer-only recipe, never an immediate toggle. These
are remembered/optimistic states, not evidence of reception or physical power.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

from .pronto_validator import validate_pronto

POWER_VARIANTS = frozenset({"on_only", "on_toggle", "off_only", "off_toggle", "cancel"})


@dataclass
class MatrixPowerPolicy:
    default_mode: str
    default_fan: str
    temperature_modes: list[str]
    fan_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    fan_power_control: bool | list[str] = False
    min_send_interval: float = 3.0

    def as_dict(self) -> dict:
        return asdict(self)

    def effective_fan(self, mode: str, fan: str) -> str:
        return self.fan_overrides.get(mode, {}).get(fan, fan)

    def fan_requests_on(self, fan: str) -> bool:
        """True enables all operating fans; a list selects explicit ON fans."""
        if isinstance(self.fan_power_control, bool):
            return self.fan_power_control
        return fan in self.fan_power_control


def power_variant(previous: bool | None, requested: bool) -> str:
    """Select at execution time. An unknown previous state must not toggle."""
    suffix = "toggle" if previous is not None and previous != requested else "only"
    return f"{'on' if requested else 'off'}_{suffix}"


def single_frame_pronto(code: object) -> bool:
    """A raw frame with no Pronto repeat section (no protocol re-encoding)."""
    return bool(
        isinstance(code, str)
        and validate_pronto(code).valid
        and int(code.split()[2], 16) > 0
        and int(code.split()[3], 16) == 0
    )


def parse_power_policy(raw: object, modes: list[str], fans: list[str], errors: list[str]):
    """Reject malformed policies rather than silently changing send semantics."""
    path = "climate.power_policy"
    if not isinstance(raw, dict):
        errors.append(f"{path}: must be an object")
        return None
    known = {
        "default_mode",
        "default_fan",
        "temperature_modes",
        "fan_overrides",
        "fan_power_control",
        "min_send_interval",
    }
    before = len(errors)
    if set(raw) - known:
        errors.append(f"{path}: unknown policy fields")
    mode, fan = raw.get("default_mode"), raw.get("default_fan")
    if mode not in modes:
        errors.append(f"{path}.default_mode: must be a declared mode")
    if fan not in fans or fan == "off":
        errors.append(f"{path}.default_fan: must be a declared operating fan")
    temperatures = raw.get("temperature_modes")
    if not isinstance(temperatures, list) or not all(t in modes for t in temperatures):
        errors.append(f"{path}.temperature_modes: must list declared modes")
    overrides = raw.get("fan_overrides", {})
    if not isinstance(overrides, dict) or any(
        m not in modes
        or not isinstance(mapping, dict)
        or any(src not in fans or dst not in fans for src, dst in mapping.items())
        for m, mapping in overrides.items()
    ):
        errors.append(f"{path}.fan_overrides: must map declared modes and fans")
    fan_power = raw.get("fan_power_control", False)
    if not isinstance(fan_power, bool) and not (
        isinstance(fan_power, list)
        and fan_power
        and all(isinstance(f, str) and f in fans and f != "off" for f in fan_power)
        and len(set(fan_power)) == len(fan_power)
    ):
        errors.append(
            f"{path}.fan_power_control: must be boolean or a nonempty list of operating fans"
        )
    interval = raw.get("min_send_interval", 3.0)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
        or not 0 <= interval <= 60
    ):
        errors.append(f"{path}.min_send_interval: must be finite, between 0 and 60 seconds")
    if len(errors) != before:
        return None
    return MatrixPowerPolicy(mode, fan, list(temperatures), overrides, fan_power, float(interval))
