"""Every channel's decoder, by (source, channel): what the live worker, replay and the corpus
tools decode polls with. One table, so a channel decodes the same everywhere.

A decoder takes the body, its sha256, the reception time, the policy's clock skew and the
poll's request context (`Poll.request`: a station board's station); a connector that needs
none of it ignores it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

from obehy.realtime.alerts import Alert
from obehy.realtime.facts import Observation
from obehy.realtime.sources import (
    arriva_express,
    dpmd,
    duk,
    mpvnet,
    pid,
    pid_alerts,
    sz,
    sz_tabule,
)
from obehy.realtime.times import Instant

Decoder = Callable[[bytes, str, Instant, timedelta, Mapping[str, Any]], list[Observation]]
DECODERS: dict[tuple[str, str], Decoder] = {
    (duk.SOURCE, duk.CHANNEL): lambda body, sha, at, skew, _: duk.decode(body, sha, at, skew),
    (sz.SOURCE, sz.CHANNEL): lambda body, sha, at, skew, _: sz.decode(body, sha, at, skew),
    (sz_tabule.SOURCE, sz_tabule.CHANNEL): sz_tabule.decode,
    (dpmd.SOURCE, dpmd.CHANNEL): lambda body, sha, at, skew, _: dpmd.decode(body, sha, at, skew),
    (pid.SOURCE, pid.CHANNEL): lambda body, sha, at, skew, _: pid.decode(body, sha, at, skew),
    (arriva_express.SOURCE, arriva_express.CHANNEL): (
        lambda body, sha, at, skew, _: arriva_express.decode(body, sha, at, skew)
    ),
    **{
        (mpvnet.SOURCE, name): (
            lambda body, sha, at, skew, _, name=name: mpvnet.decode(body, sha, at, skew, name)
        )
        for name in mpvnet.CHANNELS
    },
}

# The decoder version each channel's observations carry (`Observation.decoder_version`).
DECODER_VERSIONS: dict[tuple[str, str], int] = {
    (duk.SOURCE, duk.CHANNEL): duk.DECODER_VERSION,
    (sz.SOURCE, sz.CHANNEL): sz.DECODER_VERSION,
    (sz_tabule.SOURCE, sz_tabule.CHANNEL): sz_tabule.DECODER_VERSION,
    (dpmd.SOURCE, dpmd.CHANNEL): dpmd.DECODER_VERSION,
    (pid.SOURCE, pid.CHANNEL): pid.DECODER_VERSION,
    (arriva_express.SOURCE, arriva_express.CHANNEL): arriva_express.DECODER_VERSION,
    **{(mpvnet.SOURCE, name): mpvnet.DECODER_VERSION for name in mpvnet.CHANNELS},
}

# Alert channels (BASE_PLAN.md section 25): body, reception time. Never replayed: alerts do
# not pass through the core.
AlertDecoder = Callable[[bytes, Instant], list[Alert]]
ALERT_DECODERS: dict[tuple[str, str], AlertDecoder] = {
    (pid_alerts.SOURCE, pid_alerts.ALERTS): pid_alerts.decode_alerts,
    (pid_alerts.SOURCE, pid_alerts.INFOTEXTS): pid_alerts.decode_infotexts,
}
