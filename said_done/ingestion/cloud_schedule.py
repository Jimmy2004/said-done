"""Schedule cloud reads using the upstream server's latest received timestamp."""
import logging
import math
import time
from datetime import datetime, timezone


class CloudPollSchedule:
    # Leave time for the status round trip before the actual transcript read.
    precheck_lead = 8

    def __init__(self, client, device, interval, delay=30, clock=time.time, status_tool='sync_status'):
        self.status_tool=status_tool
        self.client, self.device = client, device
        self.interval, self.delay, self.clock = interval, delay, clock
        self.last_received = None

    def _refresh(self):
        if self.status_tool is None:
            return
        try:
            status = self.client.call(self.status_tool, {'device': self.device})
            values = []
            now = self.clock()
            for row in status['results']:
                if row.get('sourceType') != 'transcript' or row.get('device') != self.device:
                    continue
                raw = row.get('lastReceived')
                if not raw:
                    continue
                received = datetime.fromisoformat(raw)
                if received.tzinfo is None or not 0 < received.timestamp() <= now + self.delay:
                    continue
                values.append(received.timestamp())
            if values:
                self.last_received = max(values + ([self.last_received] if self.last_received is not None else []))
        except Exception as exc:
            logging.warning('cloud sync timestamp unavailable (%s); retaining poll cadence', type(exc).__name__)

    def first_deadline(self):
        self._refresh()
        return self.next_deadline()

    def next_deadline(self):
        now = self.clock()
        if self.last_received is None:
            deadline = now + self.interval
        else:
            origin = self.last_received + self.delay
            # Advance on the server's grid, without drift from request latency.
            step = max(0, math.floor((now - origin) / self.interval) + 1)
            deadline = origin + step * self.interval
        self._log(deadline)
        return deadline

    def defer_before_poll(self, scheduled):
        self._refresh()
        if self.last_received is None:
            return None
        origin = self.last_received + self.delay
        # Match this cycle to the nearest slot on the refreshed grid, rather
        # than skipping a whole cycle when the server is a few seconds late.
        step = max(0, math.floor((scheduled - origin) / self.interval + 0.5))
        corrected = origin + step * self.interval
        if corrected > max(scheduled, self.clock()):
            self._log(corrected)
            return corrected
        return None

    def _log(self, deadline):
        fmt = lambda stamp: datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        logging.info('cloud transcript poll scheduled at %s (upstream lastReceived %s)',
                     fmt(deadline), fmt(self.last_received) if self.last_received is not None else 'unavailable')
