"""Feed `quality.QualityLog` from core state the way replay does: through `journey_sample`
records in their canonical text (`emit/samples.py`, `journey_view.py`)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from obehy.realtime.canonical import line
from obehy.realtime.core import Context
from obehy.realtime.emit.samples import JourneySampler
from obehy.realtime.journey_view import JourneyView
from obehy.realtime.model import FeedState
from obehy.realtime.quality import QualityLog
from obehy.realtime.times import Instant


@dataclass(slots=True)
class StreamQuality:
    log: QualityLog = field(default_factory=QualityLog)
    sampler: JourneySampler = field(default_factory=JourneySampler)
    view: JourneyView = field(default_factory=JourneyView)

    def sample(self, state: FeedState, ctx: Context, at: Instant) -> None:
        record = json.loads(line(self.sampler.sample(state, ctx, at)))
        self.log.sample(state.feed, self.view.read(record), at)
