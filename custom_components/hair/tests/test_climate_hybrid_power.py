"""Offline POC: fake native transport only. No HA services or MQTT publications."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from unittest.mock import AsyncMock, MagicMock, patch
from unittest.mock import call as mock_call

import homeassistant.components.infrared as infrared
import pytest
from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.core import State

from custom_components.hair import tx_gate
from custom_components.hair.climate import HAIRClimateEntity, async_setup_entry
from custom_components.hair.const import DOMAIN, DeviceType
from custom_components.hair.device_manager import DeviceManager
from custom_components.hair.entity_factory import EntityFactory
from custom_components.hair.ir_command import ProntoCommand, TerminatedCommand
from custom_components.hair.matrix_store import load_matrix, write_matrix
from custom_components.hair.models import IRDevice
from custom_components.hair.storage import HAIRStore
from custom_components.hair.wig_climate import resolve_cell
from custom_components.hair.wig_format import cells_content_hash, parse_wig, serialize_wig
from poc.ac_hybrid.build_profile import (
    FANS,
    FIXTURES,
    PROFILE,
    TICK_US,
    build_profile,
    checksum,
    logical_payload,
    source_ticks,
    validate_payload,
)

PACKETS = json.loads((FIXTURES / "packets.json").read_text())
LOGICAL = json.loads((FIXTURES / "logical-frames.json").read_text())
PROFILE_DATA = json.loads(PROFILE.read_text())


def matrix():
    parsed = parse_wig(PROFILE.read_text())
    assert parsed.ok, parsed.errors
    return parsed.wig.climate


async def entity(*, power=None, mode="cool", fan="auto", temp=23, device_id="living"):
    manager = MagicMock()
    manager.async_get_matrix = AsyncMock(return_value=matrix())
    manager.async_send_matrix_cell = AsyncMock()
    device = IRDevice(
        id=device_id,
        name=device_id,
        device_type=DeviceType.AC,
        climate_matrix=True,
        emitter_entity_ids=["infrared.fake"],
    )
    result = HAIRClimateEntity(device, manager)
    result.hass = MagicMock()
    result.hass.config.units.temperature_unit = "°C"
    result.async_write_ha_state = MagicMock()
    await result.async_added_to_hass()
    result._remembered_power = power
    result._last_active_hvac_mode = HVACMode(mode)
    result._hvac_mode = HVACMode(mode) if power else HVACMode.OFF
    result._fan_mode, result._target_temperature = fan, temp
    return result, manager


def sent(manager):
    return [call.args[1].split(":")[0] for call in manager.async_send_matrix_cell.await_args_list]


@pytest.mark.parametrize(
    "previous,requested,variant",
    [
        (False, True, "on_toggle"),
        (True, True, "on_only"),
        (True, False, "off_toggle"),
        (False, False, "off_only"),
        (None, True, "on_only"),
        (None, False, "off_only"),
    ],
)
async def test_power_transitions_exact_packet(previous, requested, variant):
    e, manager = await entity(power=previous)
    await (e.async_turn_on() if requested else e.async_turn_off())
    assert sent(manager) == [variant]
    cell = next(c for c in e._matrix.cells if (c.mode, c.fan, c.temp) == ("cool", "auto", 23))
    call = manager.async_send_matrix_cell.await_args
    assert call.args[2] == cell.power_variants[variant]
    assert call.kwargs["single_frame"] is True
    assert call.kwargs["minimum_interval"] == 3
    assert e._remembered_power == requested


async def test_repeated_explicit_requests_are_not_deduplicated():
    e, manager = await entity(power=False, mode="heat", fan="high")
    for request in (e.async_turn_on, e.async_turn_on, e.async_turn_off, e.async_turn_off):
        await request()
    assert sent(manager) == ["on_toggle", "on_only", "off_toggle", "off_only"]
    assert e._last_active_hvac_mode == HVACMode.HEAT and e._fan_mode == "high"


async def test_temperature_on_off_and_noops():
    e, manager = await entity(power=False)
    await e.async_set_temperature(temperature=25)
    assert e.target_temperature == 25 and not sent(manager)
    await e.async_turn_on()
    await e.async_set_temperature(temperature=27)
    await e.async_set_temperature(temperature=27.1)  # rounds to same target
    assert sent(manager) == ["on_toggle", "on_only"]
    assert e.target_temperature == 27
    await e.async_set_temperature(temperature=99)
    assert e.target_temperature == 30
    await e.async_set_temperature(temperature=-5)
    assert e.target_temperature == 16


def test_settings_coverage_and_effective_fan_for_every_requested_combination():
    m = matrix()
    for mode in m.modes:
        for fan in FANS:
            effective = m.power_policy.effective_fan(mode, fan)
            for target in range(16, 31):
                cell = resolve_cell(m, mode, effective, None, target)
                assert cell is not None and (cell.mode, cell.fan) == (mode, effective)
                assert cell.temp == (target if mode in ("cool", "heat") else None)
                prefix = {"cool": "ar", "heat": "ah", "auto": "aa", "dry": "ad", "fan_only": "aw"}[
                    mode
                ]
                key = f"{prefix}{target if cell.temp is not None else ''}_f{FANS.index(effective)}"
                for variant, code in cell.power_variants.items():
                    ticks = [
                        round(abs(t) / TICK_US) for t in ProntoCommand(code).get_raw_timings()
                    ]
                    assert logical_payload(ticks) == bytes.fromhex(LOGICAL[f"{variant}:{key}"])


@pytest.mark.parametrize(
    "mode,fan,effective",
    [
        ("dry", "high", "low"),
        ("dry", "auto", "low"),
        ("fan_only", "auto", "low"),
        ("fan_only", "high", "high"),
        ("auto", "medium", "medium"),
    ],
)
async def test_settings_constraints_and_hidden_temperature(mode, fan, effective):
    e, manager = await entity(power=True, temp=26)
    await e.async_set_hvac_mode(HVACMode(mode))
    await e.async_set_fan_mode(fan)
    await e.async_set_temperature(temperature=19)
    assert sent(manager) == ["on_only"] * (1 if fan == "auto" else 2)
    assert e._fan_mode == effective and e._target_temperature == 26
    assert e.target_temperature is None
    assert e.swing_modes is None and e.target_temperature_step == 1
    assert not e.supported_features & ClimateEntityFeature.SWING_MODE
    await e.async_set_hvac_mode(HVACMode.COOL)
    assert e.target_temperature == 26


async def test_homekit_fan_power_restores_mode_preserves_speed():
    e, manager = await entity(power=False, mode="heat", fan="high")
    assert e.fan_mode == "off" and e.extra_state_attributes["saved_fan_mode"] == "high"
    await e.async_set_fan_mode("medium")
    assert e.hvac_mode == HVACMode.HEAT
    await e.async_set_fan_mode("medium")
    await e.async_set_fan_mode("off")
    await e.async_set_fan_mode("off")
    assert sent(manager) == ["on_toggle", "on_only", "off_toggle", "off_only"]
    assert e._fan_mode == "medium" and e.fan_mode == "off"


@pytest.mark.parametrize("power", [False, True, None])
async def test_fan_auto_preserves_power_and_only_sends_changed_on_settings(power):
    e, manager = await entity(power=power, mode="heat", fan="high", temp=26)
    await e.async_set_fan_mode("auto")
    assert e._remembered_power is power
    assert e._last_active_hvac_mode == HVACMode.HEAT
    assert (e._fan_mode, e._target_temperature) == ("auto", 26)
    assert sent(manager) == (["on_only"] if power is True else [])
    await e.async_set_fan_mode("auto")
    assert sent(manager) == (["on_only"] if power is True else [])


async def test_boolean_fan_power_policy_retains_all_fans_request_on_behavior():
    e, manager = await entity(power=False, fan="high")
    e._matrix.power_policy.fan_power_control = True
    await e.async_set_fan_mode("auto")
    await e.async_set_fan_mode("auto")
    assert e._remembered_power is True
    assert sent(manager) == ["on_toggle", "on_only"]


async def test_queued_fan_auto_uses_power_at_execution():
    e, manager = await entity(power=False, fan="high")
    entered, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()

    manager.async_send_matrix_cell.side_effect = send
    turn_on = asyncio.create_task(e.async_turn_on())
    await entered.wait()
    fan_auto = asyncio.create_task(e.async_set_fan_mode("auto"))
    release.set()
    await asyncio.gather(turn_on, fan_auto)
    assert sent(manager) == ["on_toggle", "on_only"]
    assert e._remembered_power is True and e._fan_mode == "auto"


@pytest.mark.parametrize("invalid", [[], ["off"], ["high", "high"], ["missing"], [None], [{}], {}])
def test_invalid_fan_power_policy_rejected(invalid):
    data = copy.deepcopy(PROFILE_DATA)
    data["climate"]["power_policy"]["fan_power_control"] = invalid
    parsed = parse_wig(json.dumps(data))
    assert not parsed.ok
    assert any("fan_power_control" in error for error in parsed.errors)


async def test_correction_cancels_only_and_preserves_settings():
    e, manager = await entity(power=True, mode="heat", fan="high", temp=28)
    await e.async_correct_remembered_power()
    assert sent(manager) == ["cancel"] and e.hvac_mode == HVACMode.OFF
    await e.async_correct_remembered_power()
    assert sent(manager) == ["cancel", "cancel"] and e.hvac_mode == HVACMode.HEAT
    assert (e._fan_mode, e.target_temperature) == ("high", 28)
    for call in manager.async_send_matrix_cell.await_args_list:
        timings = ProntoCommand(call.args[2]).get_raw_timings()
        payload = logical_payload([round(abs(t) / TICK_US) for t in timings])
        validate_payload(payload, "cancel")


async def test_unknown_correction_refused_and_failed_send_no_retry():
    e, manager = await entity()
    assert e.hvac_mode is None
    with pytest.raises(ValueError, match="unknown"):
        await e.async_correct_remembered_power()
    assert not sent(manager)
    manager.async_send_matrix_cell.side_effect = RuntimeError("transport failed")
    with pytest.raises(RuntimeError):
        await e.async_set_hvac_mode(HVACMode.HEAT)
    assert len(sent(manager)) == 1 and e.hvac_mode is None
    assert (e._last_active_hvac_mode, e._fan_mode, e.target_temperature) == (
        HVACMode.COOL,
        "auto",
        23,
    )
    manager.async_send_matrix_cell.side_effect = None
    await e.async_turn_off()
    assert sent(manager)[-1] == "off_only"


async def test_queued_selection_and_bookkeeping_are_atomic():
    e, manager = await entity(power=False)
    entered, release = asyncio.Event(), asyncio.Event()

    async def fake_send(*args, **kwargs):
        entered.set()
        await release.wait()

    manager.async_send_matrix_cell.side_effect = fake_send
    first = asyncio.create_task(e.async_turn_on())
    await entered.wait()
    requests = [
        asyncio.create_task(f())
        for f in (
            e.async_turn_off,
            e.async_turn_off,
            e.async_turn_on,
            e.async_correct_remembered_power,
            e.async_turn_on,
        )
    ]
    await asyncio.sleep(0)
    assert e._remembered_power is False and sent(manager) == ["on_toggle"]
    release.set()
    await asyncio.gather(first, *requests)
    assert sent(manager) == [
        "on_toggle",
        "off_toggle",
        "off_only",
        "on_toggle",
        "cancel",
        "on_toggle",
    ]
    assert e._remembered_power is True


async def test_independent_devices_and_restart_while_off_no_transmission():
    a, _am = await entity(power=True, mode="heat", fan="high", temp=29, device_id="bedrooms")
    b, bm = await entity(power=False, mode="cool", fan="medium", temp=21)
    await a.async_turn_off()
    saved = a.extra_restore_state_data
    reboot, rm = await entity(device_id="bedrooms")
    reboot.async_get_last_state = AsyncMock(return_value=State("climate.test", "off", {}))
    reboot.async_get_last_extra_data = AsyncMock(return_value=saved)
    await reboot._async_restore_state()
    assert not sent(rm)
    assert (reboot._last_active_hvac_mode, reboot._fan_mode, reboot.target_temperature) == (
        HVACMode.HEAT,
        "high",
        29,
    )
    await reboot.async_turn_on()
    assert sent(rm) == ["on_toggle"] and reboot.hvac_mode == HVACMode.HEAT
    assert not sent(bm) and (b._remembered_power, b._fan_mode, b.target_temperature) == (
        False,
        "medium",
        21,
    )


@pytest.mark.parametrize("power,mode", [(None, "cool"), (True, "dry"), (False, "fan_only")])
async def test_restart_extra_state_preserves_unknown_and_hidden_target(power, mode):
    old, _ = await entity(power=power, mode=mode, fan="low", temp=28)
    new, manager = await entity()
    new.async_get_last_state = AsyncMock(return_value=State("climate.test", "unknown", {}))
    new.async_get_last_extra_data = AsyncMock(return_value=old.extra_restore_state_data)
    await new._async_restore_state()
    assert not sent(manager)
    assert (
        new._remembered_power,
        new._last_active_hvac_mode,
        new._fan_mode,
        new._target_temperature,
    ) == (power, HVACMode(mode), "low", 28)


async def test_service_registration(fake_hass):
    entry = MagicMock(entry_id="poc")
    fake_hass.data[DOMAIN] = {
        entry.entry_id: {
            "device_manager": MagicMock(get_all_devices=MagicMock(return_value=[])),
            "entity_factory": MagicMock(),
        }
    }
    with patch("homeassistant.helpers.entity_platform.async_get_current_platform") as platform:
        await async_setup_entry(fake_hass, entry, MagicMock())
    assert platform.return_value.async_register_entity_service.call_count == 2
    platform.return_value.async_register_entity_service.assert_has_calls(
        [
            mock_call("correct_remembered_power", {}, "async_correct_remembered_power"),
        ]
    )


@pytest.mark.parametrize("packet_key", list(PACKETS))
def test_all_640_payloads_and_entire_timing_envelope(packet_key):
    variant, key = packet_key.split(":")
    prefix, fan = key.split("_f")
    file_mode = {"ar": "cool", "ah": "heat", "aa": "auto", "ad": "dry", "aw": "fan_only"}[
        prefix[:2]
    ]
    temp = int(prefix[2:]) if prefix[2:] else None
    cell = next(
        c
        for c in PROFILE_DATA["climate"]["cells"]
        if (c["mode"], c["fan"], c.get("temp")) == (file_mode, FANS[int(fan)], temp)
    )
    code = ProntoCommand(cell["power_variants"][variant])
    assert code.repeat_count == 0
    original = source_ticks(base64.b64decode(PACKETS[packet_key]))
    # Mimic the actual saved native HOBEIAN converter's quantization.
    wire = TerminatedCommand(code).get_raw_timings()
    assert wire[-1] < 0
    converted = [round(abs(t) * 269 / 8192) for t in wire[:-1]]
    assert converted == original  # every source timing, including trailer
    assert max(abs(abs(t) - tick * TICK_US) for t, tick in zip(wire, original, strict=False)) <= 14
    payload = logical_payload(converted)
    assert payload == bytes.fromhex(LOGICAL[packet_key])
    assert checksum(payload) == payload[17]
    validate_payload(payload, variant)
    # Only the timer/clock/toggle/checksum differ across variants.
    base = bytes.fromhex(LOGICAL[f"on_only:{key}"])
    assert payload[2:5] == base[2:5] and payload[9:14] == base[9:14]
    assert payload[15:17] == base[15:17]


async def test_initial_migration_memory_is_local_and_cannot_overwrite_history():
    e, manager = await entity()
    await e.async_initialize_remembered_climate(False, "heat", "high", 29)
    assert not sent(manager) and e.hvac_mode == HVACMode.OFF
    assert (e._last_active_hvac_mode, e._fan_mode, e.target_temperature) == (
        HVACMode.HEAT,
        "high",
        29,
    )
    with pytest.raises(ValueError, match="fresh"):
        await e.async_initialize_remembered_climate(True, "cool", "low", 20)
    await e.async_turn_on()
    assert sent(manager) == ["on_toggle"]


async def test_failed_first_attempt_prevents_initialization_and_interrupted_send_is_unknown():
    e, manager = await entity()
    manager.async_send_matrix_cell.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await e.async_turn_on()
    assert e.hvac_mode is None and sent(manager) == ["on_only"]
    with pytest.raises(ValueError, match="fresh"):
        await e.async_initialize_remembered_climate(False, "cool", "auto", 23)


def test_reproducible_profile_roundtrip_version_gate_and_hash_binding(tmp_path):
    assert build_profile(PACKETS, LOGICAL) == PROFILE_DATA
    parsed = parse_wig(PROFILE.read_text())
    assert parsed.ok
    roundtrip = parse_wig(serialize_wig(parsed.wig))
    assert roundtrip.ok
    assert cells_content_hash(roundtrip.wig.climate) == cells_content_hash(parsed.wig.climate)
    m = roundtrip.wig.climate
    old_hash = cells_content_hash(m)
    m.cells[0].power_variants["off_toggle"] = m.cells[1].power_variants["off_toggle"]
    assert cells_content_hash(m) != old_hash
    m = matrix()
    write_matrix(tmp_path, "poc", m)
    assert load_matrix(tmp_path, "poc").power_policy == m.power_policy
    assert '"hair-matrix/2"' in (tmp_path / "hair/matrices/poc.matrix.json").read_text()
    older = copy.deepcopy(PROFILE_DATA)
    older["format"] = "hair-wig/4"
    assert not parse_wig(json.dumps(older)).ok


@pytest.mark.parametrize(
    "defect", ["missing_variant", "repeat", "multiple_sends", "bad_policy", "swing"]
)
def test_profile_refuses_unsafe_recipes(defect):
    data = copy.deepcopy(PROFILE_DATA)
    cell = data["climate"]["cells"][0]
    if defect == "missing_variant":
        cell["power_variants"].pop("cancel")
    elif defect == "repeat":
        cell["power_variants"]["cancel"] = "0000 006D 0000 0001 0010 0010"
    elif defect == "multiple_sends":
        cell["send_count"] = 2
    elif defect == "bad_policy":
        data["climate"]["power_policy"]["default_mode"] = "unsupported"
    else:
        data["climate"]["swing_modes"] = ["swing"]
    assert not parse_wig(json.dumps(data)).ok


@pytest.fixture
def manager(fake_hass):
    store = HAIRStore(fake_hass)
    store._loaded = True
    return DeviceManager(fake_hass, store, EntityFactory(fake_hass), "entry-poc")


async def test_real_manager_calls_fake_native_transport_once_and_rejects_broadcast(manager):
    tx_gate.reset_for_test()
    device = IRDevice(
        id="dev",
        name="Fake AC",
        climate_matrix=True,
        device_type=DeviceType.AC,
        emitter_entity_ids=["infrared.fake"],
    )
    manager._store.add_device(device)
    frame = matrix().cells[0].power_variants["on_toggle"]
    transport = AsyncMock()
    with patch.object(infrared, "async_send_command", transport):
        await manager.async_send_matrix_cell(device.id, "on_toggle", frame, single_frame=True)
    assert transport.await_count == 1
    assert transport.await_args.args[1] == "infrared.fake"
    assert transport.await_args.args[2].repeat_count == 0
    device.emitter_entity_ids.append("infrared.another")
    with pytest.raises(ValueError, match="one emitter"):
        await manager.async_send_matrix_cell(device.id, "on_toggle", frame, single_frame=True)
    assert transport.await_count == 1


async def test_shared_emitter_spacing_failures_and_noop_does_not_reserve_delay(manager):
    tx_gate.reset_for_test()
    now, calls, waits = [100.0], [], []

    async def sleep(delay):
        waits.append(delay)
        now[0] += delay

    async def fake_transport(hass, emitter_id, command):
        calls.append((now[0], emitter_id, command.repeat_count))

    a, _ = await entity(power=False, device_id="a")
    b, _ = await entity(power=True, device_id="b")
    a._manager = b._manager = manager
    for e in (a, b):
        manager._store.add_device(e._device)
    with (
        patch.object(infrared, "async_send_command", fake_transport),
        patch.object(tx_gate.time, "monotonic", lambda: now[0]),
        patch.object(tx_gate.asyncio, "sleep", sleep),
    ):
        await a.async_turn_on()
        await a.async_set_temperature(temperature=23)  # no-op: no sleep
        assert waits == []
        await b.async_turn_off()
        await a.async_turn_on()
    assert calls == [
        (100, "infrared.fake", 0),
        (103, "infrared.fake", 0),
        (106, "infrared.fake", 0),
    ]
    assert waits == [3, 3]
    tx_gate.reset_for_test()


async def test_actual_failed_transport_no_retry_unknown_next_send_and_spacing(manager):
    e, _ = await entity(power=True)
    e._manager = manager
    manager._store.add_device(e._device)
    now, calls, waits = [100.0], [], []

    async def sleep(delay):
        waits.append(delay)
        now[0] += delay

    async def transport(hass, emitter_id, command):
        calls.append((now[0], command))
        if len(calls) == 1:
            raise RuntimeError("blaster failed after it may have transmitted")

    with (
        patch.object(infrared, "async_send_command", transport),
        patch.object(tx_gate.time, "monotonic", lambda: now[0]),
        patch.object(tx_gate.asyncio, "sleep", sleep),
    ):
        with pytest.raises(RuntimeError):
            await e.async_turn_off()
        assert len(calls) == 1 and e.hvac_mode is None
        await e.async_turn_on()
    assert [t for t, _ in calls] == [100, 103] and waits == [3]
    payloads = [
        logical_payload([round(abs(t) / TICK_US) for t in cmd.get_raw_timings()[:-1]])
        for _, cmd in calls
    ]
    validate_payload(payloads[0], "off_toggle")
    validate_payload(payloads[1], "on_only")
    assert all(cmd.repeat_count == 0 for _, cmd in calls)
