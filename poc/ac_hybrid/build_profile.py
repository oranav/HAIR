"""Offline-only profile export from the proven packet dictionary.

No Home Assistant imports, services, MQTT or network access. Protocol
checks live here, not in HAIR's generic climate dispatcher.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "custom_components/hair/tests/fixtures/ac_hybrid"
PROFILE = Path(__file__).with_name("k1028e-715-hybrid.wig.json")
VARIANTS = ("on_only", "on_toggle", "off_only", "off_toggle", "cancel")
# The saved HOBEIAN native converter uses round(duration_us * 269 / 8192).
# Use its inverse; do not replace the learned envelope with ELEC7024 defaults.
TICK_US = 8192 / 269
PRONTO_PERIOD_US = 109 * 0.241246  # HAIR's 38 kHz Pronto carrier word.
MODE_PREFIX = {"ar": "cool", "ah": "heat", "aa": "auto", "ad": "dry", "aw": "fan_only"}
FANS = ("auto", "low", "medium", "high")


def source_ticks(packet: bytes) -> list[int]:
    """Read this fixture's entire stored envelope, including its trailing pulse.

    These learned containers declare 291 bytes although only 287 follow the
    header. The proven builder preserved them verbatim. The final 0x99 is
    retained as a timing, following HAIR's existing Broadlink reader; we do
    not silently repair/remove that unverified part of the captured envelope.
    The native emitter will produce a well-formed transport container.
    """
    if len(packet) != 291 or packet[:4] != bytes.fromhex("26 00 23 01"):
        raise ValueError("Unexpected validated packet container")
    ticks = []
    cursor = 4
    while cursor < len(packet):
        value = packet[cursor]
        cursor += 1
        if value == 0:
            value = int.from_bytes(packet[cursor : cursor + 2], "big")
            cursor += 2
        ticks.append(value)
    assert len(ticks) == 285 and ticks[-2:] == [639, 153]
    return ticks


def logical_payload(ticks: list[int]) -> bytes:
    bits = [int(ticks[3 + 2 * i] > 20) for i in range(140)]
    return bytes(
        sum(bits[j] << (j % 8) for j in range(i, min(i + 8, 140))) for i in range(0, 140, 8)
    )


def checksum(payload: bytes) -> int:
    return sum((byte >> 4) + (byte & 15) for byte in payload[:17]) & 15


def validate_payload(payload: bytes, variant: str) -> None:
    assert len(payload) == 18 and payload[17] == checksum(payload)
    assert payload[:2] == bytes.fromhex("29 23")
    assert bool(payload[14] & 0x80) == variant.endswith("toggle")
    timer = 1 if variant.startswith("on_") else 2 if variant.startswith("off_") else 0
    assert payload[14] & 3 == timer
    assert payload[5:7] == (bytes.fromhex("23 30") if timer == 1 else b"\0\0")
    assert payload[7:9] == (bytes.fromhex("23 30") if timer == 2 else b"\0\0")


def packet_pronto(packet: bytes) -> str:
    ticks = source_ticks(packet)
    # Exactly the source's pulse envelope; nearest carrier-cycle quantization.
    words = [round(t * TICK_US / PRONTO_PERIOD_US) for t in ticks]
    words.append(0)  # odd timing count: mark-terminated, no repeat section
    return " ".join(f"{w:04X}" for w in [0, 109, len(words) // 2, 0, *words])


def build_profile(packets: dict[str, str], logical: dict[str, str]) -> dict:
    assert len(packets) == 640 and set(packets) == set(logical)
    settings = list(dict.fromkeys(key.split(":")[1] for key in packets))
    assert len(settings) == 128
    cells = []
    for key in settings:
        match = re.fullmatch(r"(ar|ah|aa|ad|aw)(\d*)_f([0-3])", key)
        assert match
        prefix, temperature, fan = match.groups()
        variants = {}
        for variant in VARIANTS:
            packet_key = f"{variant}:{key}"
            packet = base64.b64decode(packets[packet_key], validate=True)
            payload = logical_payload(source_ticks(packet))
            assert payload == bytes.fromhex(logical[packet_key])
            validate_payload(payload, variant)
            variants[variant] = packet_pronto(packet)
        cell = {
            "mode": MODE_PREFIX[prefix],
            "fan": FANS[int(fan)],
            "pronto": variants["on_only"],
            "power_variants": variants,
        }
        if temperature:
            cell["temp"] = int(temperature)
        cells.append(cell)
    return {
        "format": "hair-wig/5",
        "wig_id": "eb475d93-4b02-46a3-998a-00c08048e0f7",
        "name": "ATRC-A hybrid AC (private validated units, K-1028E code 715)",
        "brand": "Electra-related; compatibility limited to validated units",
        "model": "ATRC-A (validated through K-1028E code 715)",
        "kind": "airconditioner",
        "origin": "converted:private-validated-packets",
        "notes": (
            "Validated Bedrooms and Living room AC packets. Remote model ATRC-A, identified by "
            "the user; ELEC7024 is the Switcher Breeze naming. Captured with K-1028E code 715. "
            "Original remote unavailable. Not a claim of general Electra/Alfasmart compatibility. "
            "140 LSB-first bits; final checksum nibble only. "
            "Toggle plus native timer in one frame: "
            "clock 23:29, requested timer 23:30, opposite timer cleared. Enforcement at next AC "
            "minute tick, physically observed within approximately 60 seconds. Later commands "
            "replace pending timers. A wrong remembered state may cause a brief wrong-direction "
            "toggle. Successful send does not prove reception. Cancellation may beep. "
            "All 640 source packets preserved as 128 cells times five variants; measured envelope "
            "converted using the saved HOBEIAN native transport tick scale, no stock encoder. "
            "38 kHz, nearest Pronto carrier cycle. See adjacent README for transport limits."
        ),
        "signals": [],
        "climate": {
            "min_temp": 16,
            "max_temp": 30,
            "precision": 1,
            "modes": ["cool", "heat", "auto", "dry", "fan_only"],
            "fan_modes": list(FANS),
            "off": next(
                c
                for c in cells
                if c.get("temp") == 23 and c["mode"] == "cool" and c["fan"] == "auto"
            )["power_variants"]["off_only"],
            "power_policy": {
                "default_mode": "cool",
                "default_fan": "auto",
                "temperature_modes": ["cool", "heat"],
                "fan_overrides": {"dry": dict.fromkeys(FANS, "low"), "fan_only": {"auto": "low"}},
                "fan_power_control": ["low", "medium", "high"],
                "min_send_interval": 3,
            },
            "cells": cells,
        },
    }


def main() -> None:
    profile = build_profile(
        json.loads((FIXTURES / "packets.json").read_text()),
        json.loads((FIXTURES / "logical-frames.json").read_text()),
    )
    PROFILE.write_text(json.dumps(profile, indent=2) + "\n")
    print(f"Exported {PROFILE}: 128 settings, 640 validated codes. No transmission.")


if __name__ == "__main__":
    main()
