"""Adversarial CAS interleavings at the deferred Connect Start boundary."""

from contextlib import ExitStack
from copy import deepcopy
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from .test_executor_profile_precall import (
    NOW, environment as _shared_environment, executor, profile_model,
)

environment = _shared_environment


class RunCAS:
    """Independent persisted images: stale workers must never see shared mutations."""

    def __init__(self, run):
        self.current = deepcopy(run)

    def get(self, *args, **kwargs):
        return deepcopy(self.current)

    def save(self, run):
        if run['_version'] != self.current['_version']:
            raise executor.ConcurrentWriteError('another worker won')
        run['_version'] += 1
        self.current = deepcopy(run)


@pytest.mark.parametrize('interleave', ['schedule', 'start', 'lost_start_response'])
def test_profile_abort_after_claim_cannot_leave_connect_running(environment, interleave):
    _, oc = environment
    run, plan, cs = profile_model(precallSmsState='complete', precallSmsSentAt=NOW.isoformat())
    db = RunCAS(run)
    connect = {'state': 'Initialized'}

    def abort():
        result = executor.abort_run('p', 'r')
        assert result['status'] == 'aborted'
        # A normal abort observed Initialized and intentionally did not Stop it.
        oc.stop_campaign.assert_not_called()

    def start(_):
        if interleave != 'schedule':
            abort()
        connect['state'] = 'Running'
        if interleave == 'lost_start_response':
            raise TimeoutError('accepted Start, reply lost')

    def stop(_):
        connect['state'] = 'Stopped'

    oc.start_campaign.side_effect = start
    oc.stop_campaign.side_effect = stop
    if interleave == 'schedule':
        oc.update_campaign_schedule.side_effect = lambda *args: abort()

    with ExitStack() as stack:
        stack.enter_context(patch.object(executor, 'save_run', side_effect=db.save))
        getter = stack.enter_context(patch.object(executor, 'get_run', side_effect=db.get))
        stack.enter_context(patch.object(executor, '_get_campaign_state', side_effect=lambda _: connect['state']))
        for name in ('_stop_sms_campaign', '_delete_bucket_schedule_safe',
                     'update_plan_pending_warmup', 'unlock_plan_run'):
            stack.enter_context(patch.object(executor, name))
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
        assert db.current['status'] == 'aborted'
        assert db.current['bucketStates'][0]['campaignStates'][0]['status'] == 'cancelled'
        assert connect['state'] != 'Running'
        if interleave == 'schedule':
            oc.start_campaign.assert_not_called()
        else:
            oc.start_campaign.assert_called_once_with('connect')
            oc.stop_campaign.assert_called_once_with('connect')
            getter.assert_any_call('p', 'r', consistent_read=True)


def test_profile_confirmation_conflict_from_other_bucket_does_not_stop_active_voice(environment):
    _, oc = environment
    run, plan, cs = profile_model(precallSmsState='complete', precallSmsSentAt=NOW.isoformat())
    db = RunCAS(run)

    def other_bucket_tick(_):
        newer = db.get()
        newer['otherBucketProgress'] = 1
        db.save(newer)

    oc.start_campaign.side_effect = other_bucket_tick
    with patch.object(executor, 'save_run', side_effect=db.save), \
         patch.object(executor, 'get_run', side_effect=db.get), \
         patch.object(executor, '_get_campaign_state', return_value='Initialized'):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    oc.start_campaign.assert_called_once()
    oc.stop_campaign.assert_not_called()
    assert db.current['status'] == 'running'
    assert db.current['otherBucketProgress'] == 1


def test_profile_confirmation_failure_only_stops_superseded_campaign(environment):
    _, oc = environment
    run, plan, cs = profile_model(precallSmsState='complete', precallSmsSentAt=NOW.isoformat())
    db = RunCAS(run)

    def restart(_):
        newer = db.get()
        replacement = newer['bucketStates'][0]['campaignStates'][0]
        replacement.update(connectCampaignId='replacement', precallSmsGeneration=1)
        db.save(newer)

    oc.start_campaign.side_effect = restart
    with patch.object(executor, 'save_run', side_effect=db.save), \
         patch.object(executor, 'get_run', side_effect=db.get), \
         patch.object(executor, '_get_campaign_state', return_value='Initialized'):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    oc.stop_campaign.assert_called_once_with('connect')
    assert db.current['bucketStates'][0]['campaignStates'][0]['connectCampaignId'] == 'replacement'


def test_profile_missing_update_schedule_permission_never_counts_as_start(environment):
    _, oc = environment
    run, plan, cs = profile_model(precallSmsState='complete', precallSmsSentAt=NOW.isoformat())
    oc.update_campaign_schedule.side_effect = ClientError(
        {'Error': {'Code': 'AccessDeniedException', 'Message': 'missing permission'}},
        'UpdateCampaignSchedule',
    )
    with patch.object(executor, '_get_campaign_state', return_value='Initialized'):
        for _ in range(4):
            executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    assert cs['status'] == 'running'
    assert not cs.get('precallVoiceStartAttempts')
    assert oc.update_campaign_schedule.call_count == 4
    oc.start_campaign.assert_not_called()


def test_run_store_strong_read_is_scoped_and_reaches_dynamodb():
    import store
    with patch.object(store, '_table') as table:
        table.return_value.get_item.return_value = {}
        assert store.get_run('p', 'r') is None
        table.return_value.get_item.assert_called_once_with(Key={'pk': 'PLAN#p', 'sk': 'RUN#r'})
        table.return_value.get_item.reset_mock()
        assert store.get_run('p', 'r', consistent_read=True) is None
        table.return_value.get_item.assert_called_once_with(
            Key={'pk': 'PLAN#p', 'sk': 'RUN#r'}, ConsistentRead=True)


def test_profile_cold_create_conflict_preserves_other_bucket_and_created_id(environment):
    from .test_executor_profile_precall import creation_patches, provider_result
    client, oc = environment
    run, plan, cs = profile_model(status='queued', connectCampaignId=None)
    run['bucketStates'].append({'status': 'running', 'campaignStates': [], 'progress': 0})
    plan['buckets'].append({'campaigns': []})
    original_bucket = run['bucketStates'][0]
    db = RunCAS(run)

    def create(**kwargs):
        other = db.get()
        other['bucketStates'][1]['progress'] = 7
        db.save(other)
        return {'id': 'new-connect'}

    oc.create_campaign.side_effect = create
    client.invoke.return_value = provider_result(sent=2)
    with ExitStack() as stack:
        creation_patches(stack)
        stack.enter_context(patch.object(executor, 'save_run', side_effect=db.save))
        stack.enter_context(patch.object(executor, 'get_run', side_effect=db.get))
        assert executor._dispatch_ready_campaigns(run, plan, 0)
    oc.create_campaign.assert_called_once()
    oc.delete_campaign.assert_not_called()
    oc.start_campaign.assert_called_once_with('new-connect')
    assert db.current['bucketStates'][0]['campaignStates'][0]['connectCampaignId'] == 'new-connect'
    assert db.current['bucketStates'][1]['progress'] == 7
    assert run['bucketStates'][0] is original_bucket
    assert run['bucketStates'][0]['campaignStates'][0] is cs
    assert cs['status'] == 'running'


def test_profile_cold_create_cleanup_does_not_delete_an_adopted_running_id(environment):
    from .test_executor_profile_precall import creation_patches
    client, oc = environment
    run, plan, cs = profile_model(status='queued', connectCampaignId=None)
    db = RunCAS(run)

    def create(**kwargs):
        adopted = db.get()
        adopted['bucketStates'][0]['campaignStates'][0].update(
            status='running', connectCampaignId='new-connect', precallSmsState='complete',
            precallVoiceStartedAt=NOW.isoformat())
        db.save(adopted)
        return {'id': 'new-connect'}

    oc.create_campaign.side_effect = create
    with ExitStack() as stack:
        creation_patches(stack)
        stack.enter_context(patch.object(executor, 'save_run', side_effect=db.save))
        stack.enter_context(patch.object(executor, 'get_run', side_effect=db.get))
        assert executor._dispatch_ready_campaigns(run, plan, 0)
    oc.delete_campaign.assert_not_called()
    oc.start_campaign.assert_not_called()
    client.invoke.assert_not_called()
    assert db.current['bucketStates'][0]['campaignStates'][0]['precallVoiceStartedAt']


def test_profile_cold_create_cleanup_preserves_newer_generation(environment):
    from .test_executor_profile_precall import creation_patches
    client, oc = environment
    run, plan, cs = profile_model(status='queued', connectCampaignId=None)
    db = RunCAS(run)

    def create(**kwargs):
        replacement = db.get()
        replacement['bucketStates'][0]['campaignStates'][0].update(
            status='creating', connectCampaignId=None, precallSmsGeneration=1)
        db.save(replacement)
        return {'id': 'old-unclaimed-connect'}

    oc.create_campaign.side_effect = create
    with ExitStack() as stack:
        creation_patches(stack)
        stack.enter_context(patch.object(executor, 'save_run', side_effect=db.save))
        stack.enter_context(patch.object(executor, 'get_run', side_effect=db.get))
        with pytest.raises(executor.ConcurrentWriteError):
            executor._dispatch_ready_campaigns(run, plan, 0)
    oc.delete_campaign.assert_called_once_with('old-unclaimed-connect')
    oc.stop_campaign.assert_not_called()
    oc.start_campaign.assert_not_called()
    client.invoke.assert_not_called()
    assert db.current['bucketStates'][0]['campaignStates'][0]['precallSmsGeneration'] == 1
    assert db.current['bucketStates'][0]['campaignStates'][0]['connectCampaignId'] is None


@pytest.mark.parametrize('failure', [RuntimeError('reaper heartbeat stale'), TimeoutError('intent write failed')])
def test_profile_start_requires_durable_registration(environment, failure):
    _, oc = environment
    run, plan, cs = profile_model(precallSmsState='complete', precallSmsSentAt=NOW.isoformat())
    with patch('profile_voice_cleanup.register_start', side_effect=failure) as register, \
         patch.object(executor, '_get_campaign_state', return_value='Initialized'):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    register.assert_called_once_with(run, cs, now=int(NOW.timestamp()))
    oc.update_campaign_schedule.assert_not_called()
    oc.start_campaign.assert_not_called()
    assert not cs.get('precallVoiceStartedAt')


def test_profile_start_registers_before_schedule_and_checks_window_after_final_cas(environment):
    _, oc = environment
    run, plan, cs = profile_model(precallSmsState='complete', precallSmsSentAt=NOW.isoformat())
    db = RunCAS(run)
    trace = []
    intents = {}

    def save(item):
        db.save(item)
        trace.append('save')

    def register(item, state, *, now):
        trace.append('register')
        assert db.current['_version'] == item['_version']
        assert db.current['bucketStates'][0]['campaignStates'][0]['precallVoiceStartClaimedAt']
        intents[state['connectCampaignId']] = {
            'planId': item['planId'], 'runId': item['runId'],
            'connectCampaignId': state['connectCampaignId'], 'workerExpiresAt': now + 360,
        }
        return intents[state['connectCampaignId']]

    def check(intent, *, now):
        assert intent is intents['connect']
        trace.append('check')

    oc.update_campaign_schedule.side_effect = lambda *args: trace.append('schedule')
    oc.start_campaign.side_effect = lambda *args: trace.append('start')
    with patch.object(executor, 'save_run', side_effect=save), \
         patch('profile_voice_cleanup.register_start', side_effect=register), \
         patch('profile_voice_cleanup.check_start_window', side_effect=check), \
         patch.object(executor, '_get_campaign_state', return_value='Initialized'):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    assert trace == ['save', 'register', 'schedule', 'check', 'save', 'check', 'start', 'save']
    assert intents['connect']['workerExpiresAt'] == int(NOW.timestamp()) + 360
    assert db.current['bucketStates'][0]['campaignStates'][0]['precallVoiceStartedAt']


def test_profile_expired_worker_cannot_start_after_slow_schedule_update(environment):
    from datetime import timedelta
    _, oc = environment
    run, plan, cs = profile_model(precallSmsState='complete', precallSmsSentAt=NOW.isoformat())
    intent = {'workerExpiresAt': int(NOW.timestamp()) + 360}

    def delayed_update(*args):
        executor._now_utc.return_value = NOW + timedelta(seconds=361)

    def check(item, *, now):
        assert item is intent
        assert now == int(NOW.timestamp()) + 361
        raise TimeoutError('worker lease expired')

    oc.update_campaign_schedule.side_effect = delayed_update
    with patch('profile_voice_cleanup.register_start', return_value=intent), \
         patch('profile_voice_cleanup.check_start_window', side_effect=check) as checker, \
         patch.object(executor, '_get_campaign_state', return_value='Initialized'):
        executor._fire_precall_sms_for_campaign(run, plan, 0, 0)
    checker.assert_called_once()
    oc.start_campaign.assert_not_called()
    assert not cs.get('precallVoiceStartedAt')


@pytest.mark.parametrize('mode', [None, 'manual', 'disabled_profile'])
def test_legacy_and_disabled_voice_do_not_register_cleanup_intents(environment, mode):
    from .test_executor_profile_precall import creation_patches, response
    from .test_executor_sms_pending import model
    client, oc = environment
    run, plan, _ = model(status='queued', connectCampaignId=None)
    precall = plan['buckets'][0]['campaigns'][0]['campaignConfig']['precallSms']
    if mode == 'disabled_profile':
        precall.update(enabled=False, mode='profile', catalogVersion='phase1-v1')
    elif mode:
        precall['mode'] = mode
    oc.create_campaign.return_value = {'id': 'new-connect'}
    client.invoke.return_value = response({'enqueued': 2})
    with ExitStack() as stack:
        creation_patches(stack)
        register = stack.enter_context(patch('profile_voice_cleanup.register_start'))
        executor._start_one_campaign(run, plan, 0, 0)
    register.assert_not_called()
    oc.start_campaign.assert_called_once_with('new-connect')
