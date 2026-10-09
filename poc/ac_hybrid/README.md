# ATRC-A power-toggle trial

This opt-in proof of concept adds climate power variants to HAIR. It uses
Home Assistant's native infrared emitter API, including existing MQTT
infrared entities. No additional transmitter bridge or runtime dependency
is required. Ordinary HAIR profiles keep their previous behavior.

The remote model is ATRC-A; ELEC7024 is the Switcher Breeze naming. These
640 packets came from a K-1028E universal remote, code 715, and are validated
only for the original two AC units. Compatibility with other units is not
established. Stock protocol timing is not substituted.

Import `k1028e-715-hybrid.wig.json` into HAIR, then adopt it as an AC with
one infrared emitter. Loading, importing and restoration transmit nothing.
Initial power is unknown; an explicit power request uses a timer-only frame
until remembered state is established. `hair.initialize_remembered_climate`
can copy existing optimistic memory into a fresh entity without transmitting.

| Remembered power | Requested power | Frame |
| --- | --- | --- |
| OFF | ON | on_toggle |
| ON | ON | on_only |
| ON | OFF | off_toggle |
| OFF | OFF | off_only |
| Unknown | ON/OFF | corresponding timer-only frame |

Each frame combines an optional toggle with an AC-native timer: clock 23:29,
desired deadline 23:30, opposite timer disabled and cleared. Historical
physical observations found enforcement at the next internal minute tick,
within approximately 60 seconds. A wrong remembered state can cause a brief
wrong-direction toggle. Successful transmission does not establish reception
or physical power. No HA retry or delayed power command is scheduled.

Repeated explicit ON/OFF requests send timer-only frames when memory already
matches. Settings changes while ON use on_only. Temperature changes while
OFF remain local. Cool/Heat targets are integer 16–30°C; Dry uses low fan;
Fan-only auto maps to low. Other modes preserve the saved Cool/Heat target
without exposing an adjustable temperature. Manual fan speeds request ON
in the saved mode; Fan Auto preserves power; fan Off requests OFF while
preserving the saved operating speed. Swing is unsupported.

`hair.correct_remembered_power` sends one cancel frame, without a toggle,
clearing both timers, then flips only optimistic power memory. It preserves
settings, does not retransmit and may make the AC beep. Unknown memory cannot
be flipped. A transmission error makes memory unknown and is not retried.

Selection, transmission and bookkeeping are serialized. Actual sends are at
least three seconds apart, including devices sharing an emitter; local no-op
writes consume no send interval. Every policy command is one frame with zero
repeats. Use native climate actions and the two HAIR actions for this profile.

The focused fake-emitter tests cover all transitions, queues, independent
devices, restoration, failures and all 640 packet payload/timing round trips.
These are offline tests, not new hardware validation. The test fixture data
contains only IR packets and the historical public converter source. This
public package excludes live HA configuration snapshots and trial logs.

See `docs/wig-format.md` for the opt-in schema. Power-policy profiles require
hair-wig/5 and hair-matrix/2; existing profiles retain their older formats.
