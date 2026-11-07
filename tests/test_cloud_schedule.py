"""Tests for the server-aligned cloud poll schedule."""
from datetime import datetime

from said_done.ingestion.cloud_schedule import CloudPollSchedule


def stamp(s):
    return datetime.fromisoformat(f'2026-11-06T{s}+08:00').timestamp()


class Status:
    def __init__(self, received='15:30:15'):
        self.received = received

    def call(self, tool, args):
        assert tool == 'sync_status' and args == {'device': ''}
        if self.received is None:
            raise OSError('unavailable')
        return {'results': [
            {'device': '', 'sourceType': 'transcript', 'latestItem': 'ignored',
             'lastReceived': f'2026-11-06T{self.received}+08:00'},
            {'device': '', 'sourceType': 'todo',
             'lastReceived': '2026-11-06T15:31:12+08:00'},
        ]}


def test_tracks_server_cadence_without_accumulating_tick_latency():
    now = [stamp('15:31:00')]
    service = Status()
    schedule = CloudPollSchedule(service, '', 900, clock=lambda: now[0])
    first = schedule.first_deadline()
    assert first == stamp('15:45:45')
    now[0] = first - schedule.precheck_lead
    assert schedule.defer_before_poll(first) is None
    now[0] = stamp('15:46:10')  # Actual poll and processing took 25 s.
    assert schedule.next_deadline() == stamp('16:00:45')


def test_new_sync_pushes_current_poll_back_to_full_thirty_second_buffer():
    now = [stamp('15:31:00')]
    service = Status()
    schedule = CloudPollSchedule(service, '', 900, clock=lambda: now[0])
    planned = schedule.first_deadline()
    now[0] = planned - schedule.precheck_lead
    service.received = '15:45:25'
    corrected = schedule.defer_before_poll(planned)
    assert corrected == stamp('15:45:55')
    now[0] = corrected - schedule.precheck_lead
    assert schedule.defer_before_poll(corrected) is None
    now[0] = corrected
    assert schedule.next_deadline() == stamp('16:00:55')


def test_temporary_status_failure_keeps_last_alignment():
    now = [stamp('15:31:00')]
    service = Status()
    schedule = CloudPollSchedule(service, '', 900, clock=lambda: now[0])
    due = schedule.first_deadline()
    now[0] = due
    service.received = None
    assert schedule.defer_before_poll(due) is None
    assert schedule.next_deadline() == stamp('16:00:45')
    fresh = CloudPollSchedule(service, '', 900, clock=lambda: now[0])
    assert fresh.first_deadline() == now[0] + 900
