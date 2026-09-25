"""Durable voice cleanup with independent persisted snapshots and CAS failures."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import sys

import pytest
from botocore.exceptions import ClientError

from .test_executor_profile_precall import NOW, executor, profile_model
import profile_voice_cleanup as cleanup


def conditional_error():
    return ClientError({'Error': {'Code': 'ConditionalCheckFailedException'}}, 'Write')


class FakeTable:
    """Apply the module's ownership/version/lease conditions against real rows."""

    def __init__(self):
        self.rows = {}
        self.calls = []
        self.before = None
        self.after = None

    @staticmethod
    def key(item):
        return item['pk'], item['sk']

    def _hook(self, stage, operation, kwargs):
        self.calls.append((stage, operation, deepcopy(kwargs)))
        hook = getattr(self, stage)
        if hook:
            hook(operation, kwargs)

    def get_item(self, **kwargs):
        self._hook('before', 'get', kwargs)
        assert kwargs.get('ConsistentRead') is True
        row = self.rows.get(self.key(kwargs['Key']))
        result = {'Item': deepcopy(row)} if row is not None else {}
        self._hook('after', 'get', kwargs)
        return result

    def _condition(self, current, kwargs):
        condition = kwargs.get('ConditionExpression')
        if not condition:
            return
        values = kwargs.get('ExpressionAttributeValues', {})
        row = current or {}
        if condition.startswith('attribute_not_exists(pk)'):
            valid = (current is None or (
                row.get('ownerToken') == values[':owner'] and row.get('state') == values[':owned']
                and row.get('workerExpiresAt', 0) <= values[':expires']))
        elif condition.startswith('attribute_not_exists(leaseUntil)'):
            valid = 'leaseUntil' not in row or row['leaseUntil'] < values[':now']
        elif condition == 'leaseToken = :token':
            valid = row.get('leaseToken') == values[':token']
        elif condition == 'ownerToken = :owner':
            valid = row.get('ownerToken') == values[':owner']
        elif condition.startswith('#version = :version'):
            valid = row.get('version') == values[':version']
            if ':owned' in values:
                valid = valid and row.get('state') in {values[':owned'], values[':cleaning']}
            else:
                valid = (valid and row.get('state') == values[':cleaning']
                         and row.get('workerExpiresAt', 0) <= values[':now'])
        else:
            raise AssertionError(f'Unimplemented real condition: {condition}')
        if not valid:
            raise conditional_error()

    def put_item(self, **kwargs):
        self._hook('before', 'put', kwargs)
        key = self.key(kwargs['Item'])
        self._condition(self.rows.get(key), kwargs)
        self.rows[key] = deepcopy(kwargs['Item'])
        self._hook('after', 'put', kwargs)
        return {}

    def update_item(self, **kwargs):
        self._hook('before', 'update', kwargs)
        key = self.key(kwargs['Key'])
        self._condition(self.rows.get(key), kwargs)
        row = deepcopy(self.rows.get(key, kwargs['Key']))
        names = kwargs.get('ExpressionAttributeNames', {})
        values = kwargs.get('ExpressionAttributeValues', {})
        expression = kwargs['UpdateExpression']
        set_part, _, remove_part = expression.partition(' REMOVE ')
        if expression.startswith('REMOVE '):
            set_part, remove_part = '', expression[len('REMOVE '):]
        if set_part:
            assert set_part.startswith('SET ')
            for assignment in set_part[len('SET '):].split(','):
                name, value = (part.strip() for part in assignment.split('='))
                row[names.get(name, name)] = deepcopy(values[value])
        for name in remove_part.split(','):
            name = name.strip()
            if name:
                row.pop(names.get(name, name), None)
        self.rows[key] = row
        self._hook('after', 'update', kwargs)
        return {'Attributes': deepcopy(row)}

    def delete_item(self, **kwargs):
        self._hook('before', 'delete', kwargs)
        key = self.key(kwargs['Key'])
        self._condition(self.rows.get(key), kwargs)
        self.rows.pop(key, None)
        self._hook('after', 'delete', kwargs)
        return {}

    def query(self, **kwargs):
        self._hook('before', 'query', kwargs)
        assert kwargs.get('ConsistentRead') is True
        # Validate the actual Query key expression, including the campaign prefix.
        expression = kwargs['KeyConditionExpression'].get_expression()
        pk, sk = expression['values']
        assert pk.get_expression()['values'][0].name == 'pk'
        assert pk.get_expression()['values'][1] == cleanup._ACTIVE_PK
        assert sk.get_expression()['values'][0].name == 'sk'
        assert sk.get_expression()['values'][1] == 'CAMPAIGN#'
        rows = sorted((deepcopy(row) for key, row in self.rows.items()
                       if key[0] == cleanup._ACTIVE_PK and key[1].startswith('CAMPAIGN#')),
                      key=lambda row: row['sk'])
        cursor = kwargs.get('ExclusiveStartKey')
        if cursor:
            rows = [row for row in rows if row['sk'] > cursor['sk']]
        page = rows[:kwargs['Limit']]
        result = {'Items': page}
        if len(rows) > len(page):
            result['LastEvaluatedKey'] = {key: page[-1][key] for key in ('pk', 'sk')}
        self._hook('after', 'query', kwargs)
        return result


class FakeConnect:
    def __init__(self):
        self.states = {'connect': 'Initialized'}
        self.calls = []
        self.before = {}
        self.after = {}

    def _call(self, operation, campaign_id):
        self.calls.append((operation, campaign_id))
        hook = self.before.get(operation)
        if hook:
            return hook(campaign_id)
        return None

    def _done(self, operation, campaign_id):
        hook = self.after.get(operation)
        if hook:
            hook(campaign_id)

    def get_campaign_state(self, campaign_id=None, *, id=None):
        cid = id or campaign_id
        override = self._call('get', cid)
        if override is not None:
            return override
        if cid not in self.states:
            raise ClientError({'Error': {'Code': 'ResourceNotFoundException'}}, 'GetCampaignState')
        return {'state': self.states[cid]}

    def update_campaign_schedule(self, campaign_id, schedule):
        self._call('schedule', campaign_id)
        self._done('schedule', campaign_id)

    def start_campaign(self, campaign_id=None, *, id=None):
        cid = id or campaign_id
        self._call('start', cid)
        assert self.states[cid] in {'Initialized', 'Stopped'}
        self.states[cid] = 'Running'
        self._done('start', cid)

    def stop_campaign(self, campaign_id=None, *, id=None):
        cid = id or campaign_id
        self._call('stop', cid)
        if self.states.get(cid) not in {'Running', 'Paused'}:
            raise ClientError({'Error': {'Code': 'InvalidCampaignStateException'}}, 'StopCampaign')
        self.states[cid] = 'Stopped'
        self._done('stop', cid)

    def delete_campaign(self, campaign_id=None, *, id=None):
        cid = id or campaign_id
        self._call('delete', cid)
        assert self.states.get(cid) in {'Initialized', 'Stopped', 'Failed', 'Completed'}
        self.states.pop(cid)
        self._done('delete', cid)


class Runtime:
    def __init__(self):
        self.now = int(NOW.timestamp())
        self.table = FakeTable()
        self.connect = FakeConnect()
        self.table.rows[(cleanup._ACTIVE_PK, cleanup._CONTROL_SK)] = {
            'pk': cleanup._ACTIVE_PK, 'sk': cleanup._CONTROL_SK,
            'schemaVersion': 1, 'heartbeatAt': self.now,
        }
        run, _, _ = profile_model(precallSmsState='complete', precallSmsSentAt=NOW.isoformat())
        self.put_run(run)

    def put_run(self, run):
        self.table.rows[(f"PLAN#{run['planId']}", f"RUN#{run['runId']}")] = deepcopy(run)

    def get_run(self, plan_id='p', run_id='r', **kwargs):
        row = self.table.rows.get((f'PLAN#{plan_id}', f'RUN#{run_id}'))
        return deepcopy(row) if row else None

    def save_run(self, run):
        current = self.get_run(run['planId'], run['runId'])
        if current['_version'] != run['_version']:
            raise executor.ConcurrentWriteError('another worker won')
        run['_version'] += 1
        self.put_run(run)

    def cs(self, run=None):
        return (run or self.get_run())['bucketStates'][0]['campaignStates'][0]

    def intent(self, connect_id='connect'):
        return self.table.rows.get((cleanup._INTENT_PK, f'CAMPAIGN#{connect_id}'))

    def indexed(self, connect_id='connect'):
        return (cleanup._ACTIVE_PK, f'CAMPAIGN#{connect_id}') in self.table.rows

    def register(self):
        run = self.get_run()
        return cleanup.register_start(run, self.cs(run), now=self.now)

    def abort_durable(self):
        run = self.get_run()
        run['status'] = 'aborted'
        self.cs(run)['status'] = 'cancelled'
        self.save_run(run)

    def gate(self):
        run = self.get_run()
        executor._fire_precall_sms_for_campaign(run, run['planSnapshot'], 0, 0)
        return run


@pytest.fixture
def runtime(monkeypatch):
    rt = Runtime()
    monkeypatch.setenv('PROFILE_VOICE_CLEANUP_ENABLED', 'true')
    with ExitStack() as stack:
        for name, value in (('_table', rt.table), ('_connect', rt.connect)):
            stack.enter_context(patch.object(cleanup, name, return_value=value))
        stack.enter_context(patch.object(cleanup, '_now', side_effect=lambda: rt.now))
        stack.enter_context(patch.object(executor, 'get_run', side_effect=rt.get_run))
        stack.enter_context(patch.object(executor, 'save_run', side_effect=rt.save_run))
        stack.enter_context(patch.object(executor, '_now_utc', side_effect=lambda: datetime.fromtimestamp(rt.now, timezone.utc)))
        stack.enter_context(patch.object(executor, '_now_iso', side_effect=lambda: datetime.fromtimestamp(rt.now, timezone.utc).isoformat()))
        stack.enter_context(patch.dict(sys.modules, {
            'vip_shared.infrastructure.persistence.outbound_campaigns_client': MagicMock(
                build=MagicMock(return_value=rt.connect)),
        }))
        for name in ('_stop_sms_campaign', '_delete_bucket_schedule_safe',
                     'update_plan_pending_warmup', 'unlock_plan_run'):
            stack.enter_context(patch.object(executor, name))
        yield rt


@pytest.mark.parametrize('failure', ['hard_crash', 'compensation_read', 'compensation_stop'])
def test_independent_reaper_recovers_aborted_start_after_previous_p1_failures(runtime, failure):
    rt = runtime
    failure_left = [True]

    def before_start(_):
        assert rt.intent() and rt.indexed()
        assert executor.abort_run('p', 'r')['status'] == 'aborted'

    def after_start(_):
        if failure == 'hard_crash':
            raise SystemExit('worker killed after accepted Start')

    def get_run(*args, **kwargs):
        if failure == 'compensation_read' and kwargs.get('consistent_read') and failure_left[0]:
            failure_left[0] = False
            raise RuntimeError('temporary get failure')
        return rt.get_run(*args, **kwargs)

    def stop(_):
        if failure == 'compensation_stop' and failure_left[0]:
            failure_left[0] = False
            raise RuntimeError('temporary Stop failure')

    rt.connect.before['start'] = before_start
    rt.connect.after['start'] = after_start
    rt.connect.before['stop'] = stop
    with patch.object(executor, 'get_run', side_effect=get_run):
        if failure == 'hard_crash':
            with pytest.raises(SystemExit):
                rt.gate()
        else:
            rt.gate()
    assert rt.get_run()['status'] == 'aborted'
    assert rt.connect.states['connect'] == 'Running'
    assert rt.indexed()
    # No bucket tick, latest-run lookup or repeated abort is involved.
    cleanup.reap()
    assert rt.connect.states['connect'] == 'Stopped'
    assert rt.indexed()  # An in-flight worker could still finish a late Start.
    rt.now += cleanup.WORKER_WINDOW_SECONDS + 1
    cleanup.reap()
    assert rt.intent()['state'] == 'RETIRED'
    assert not rt.indexed()


def test_successful_start_keeps_intent_for_later_failed_abort(runtime):
    rt = runtime
    rt.gate()
    assert rt.cs()['precallVoiceStartedAt']
    assert rt.intent()['state'] == 'OWNED' and rt.indexed()
    cleanup.reap()
    assert ('stop', 'connect') not in rt.connect.calls
    rt.abort_durable()
    cleanup.reap()
    assert rt.connect.states['connect'] == 'Stopped'


@pytest.mark.parametrize('invalid', ['disabled', 'missing', 'stale', 'future', 'schema'])
def test_unready_reaper_never_allows_start(runtime, monkeypatch, invalid):
    rt = runtime
    control = rt.table.rows[(cleanup._ACTIVE_PK, cleanup._CONTROL_SK)]
    if invalid == 'disabled':
        monkeypatch.delenv('PROFILE_VOICE_CLEANUP_ENABLED')
    elif invalid == 'missing':
        rt.table.rows.pop((cleanup._ACTIVE_PK, cleanup._CONTROL_SK))
    elif invalid == 'stale':
        control['heartbeatAt'] = rt.now - 181
    elif invalid == 'future':
        control['heartbeatAt'] = rt.now + 31
    else:
        control['schemaVersion'] = 999
    rt.gate()
    assert ('start', 'connect') not in rt.connect.calls
    assert not rt.cs().get('precallVoiceStartedAt')


def test_index_write_failure_after_owner_write_cannot_start(runtime):
    rt = runtime
    def fail_index(operation, kwargs):
        if operation == 'put' and kwargs['Item']['pk'] == cleanup._ACTIVE_PK:
            raise RuntimeError('index unavailable')
    rt.table.before = fail_index
    rt.gate()
    assert rt.intent()['state'] == 'OWNED'
    assert not rt.indexed()
    assert ('start', 'connect') not in rt.connect.calls


def test_registration_identity_and_deadline_cannot_regress(runtime):
    rt = runtime
    original = rt.register()
    assert 'ttl' not in original
    assert rt.indexed()
    rt.now += 10
    renewed = rt.register()
    assert renewed['workerExpiresAt'] > original['workerExpiresAt']
    assert renewed['version'] != original['version']
    run = rt.get_run()
    with pytest.raises(cleanup.CleanupUnavailable):
        cleanup.register_start(run, rt.cs(run), now=rt.now - 1)
    rt.cs(run)['precallSmsGeneration'] = 1
    with pytest.raises(cleanup.CleanupUnavailable):
        cleanup.register_start(run, rt.cs(run), now=rt.now)
    assert rt.intent()['workerExpiresAt'] == renewed['workerExpiresAt']


def test_fenced_or_retired_id_cannot_be_registered_or_started_again(runtime):
    rt = runtime
    intent = rt.register()
    rt.abort_durable()
    cleanup.reap()
    assert rt.intent()['state'] == 'CLEANING'
    with pytest.raises(cleanup.CleanupUnavailable):
        rt.register()
    with pytest.raises(cleanup.CleanupUnavailable):
        cleanup.check_start_window(intent, now=rt.now)
    rt.now += cleanup.WORKER_WINDOW_SECONDS + 1
    cleanup.reap()
    assert rt.intent()['state'] == 'RETIRED'
    assert 'connect' not in rt.connect.states
    with pytest.raises(cleanup.CleanupUnavailable):
        rt.register()


def test_terminal_observation_does_not_retire_a_live_worker(runtime):
    rt = runtime
    rt.register()
    rt.abort_durable()
    rt.connect.states['connect'] = 'Stopped'
    cleanup.reap()
    assert rt.indexed()
    rt.connect.start_campaign('connect')  # Simulate acceptance of an already in-flight Start.
    cleanup.reap()
    assert rt.connect.states['connect'] == 'Stopped'
    assert rt.indexed()
    rt.now += cleanup.WORKER_WINDOW_SECONDS + 1
    cleanup.reap()
    assert not rt.indexed()


@pytest.mark.parametrize('failure', ['get_run', 'get_state', 'stop'])
def test_reaper_transient_failure_preserves_obligation_and_recovers(runtime, failure):
    rt = runtime
    rt.register()
    rt.abort_durable()
    rt.connect.states['connect'] = 'Running'
    failed = [False]
    def table_failure(operation, kwargs):
        if failure == 'get_run' and operation == 'get' and kwargs['Key']['pk'].startswith('PLAN#') and not failed[0]:
            failed[0] = True
            raise RuntimeError('read temporarily unavailable')
    def client_failure(_):
        if not failed[0]:
            failed[0] = True
            raise RuntimeError('Connect temporarily unavailable')
    rt.table.before = table_failure
    if failure != 'get_run':
        rt.connect.before['get' if failure == 'get_state' else 'stop'] = client_failure
    assert cleanup.reap()['counts']['failed'] == 1
    assert rt.indexed()
    cleanup.reap()
    assert rt.connect.states['connect'] == 'Stopped'


def test_old_reaper_snapshot_cannot_fence_newly_extended_registration(runtime):
    rt = runtime
    original = rt.register()
    original_run = rt.get_run()
    rt.abort_durable()
    rt.connect.states['connect'] = 'Running'
    renewed = []
    def before_fence(operation, kwargs):
        if operation == 'update' and kwargs['Key']['pk'] == cleanup._INTENT_PK and not renewed:
            rt.table.before = None
            renewed.append(cleanup.register_start(original_run, rt.cs(original_run), now=rt.now + 1))
    rt.table.before = before_fence
    assert cleanup.reap()['counts']['changed'] == 1
    assert rt.connect.states['connect'] == 'Running'
    assert rt.intent()['version'] == renewed[0]['version'] != original['version']
    cleanup.reap()
    assert rt.connect.states['connect'] == 'Stopped'


def test_pagination_reaches_obsolete_rows_after_active_rows(runtime, monkeypatch):
    rt = runtime
    rt.table.rows.pop(('PLAN#p', 'RUN#r'))
    monkeypatch.setattr(cleanup, '_PAGE_SIZE', 2)
    for n in range(4):
        run, _, cs = profile_model(precallSmsState='complete')
        run['runId'] = f'r{n}'
        cs['connectCampaignId'] = f'connect{n}'
        rt.put_run(run)
        rt.connect.states[cs['connectCampaignId']] = 'Running'
        cleanup.register_start(run, cs, now=rt.now)
        if n == 3:
            run['status'] = 'aborted'
            cs['status'] = 'cancelled'
            rt.put_run(run)
    assert cleanup.reap()['counts'] == {'active': 2}
    assert cleanup.reap()['counts'] == {'active': 1, 'waiting_worker': 1}
    assert rt.connect.states['connect3'] == 'Stopped'
    assert all(rt.connect.states[f'connect{n}'] == 'Running' for n in range(3))


@pytest.mark.parametrize('change', ['deleted_run', 'restart', 'new_latest'])
def test_obsolete_old_run_cleanup_never_stops_replacement(runtime, change):
    rt = runtime
    rt.register()
    rt.connect.states['connect'] = 'Running'
    replacement = rt.get_run()
    rt.cs(replacement).update(connectCampaignId='replacement', precallSmsGeneration=1)
    rt.connect.states['replacement'] = 'Running'
    if change == 'restart':
        rt.put_run(replacement)
    else:
        replacement['runId'] = 'new-run'
        rt.put_run(replacement)
        if change == 'deleted_run':
            rt.table.rows.pop(('PLAN#p', 'RUN#r'))
        else:
            rt.abort_durable()
    cleanup.reap()
    assert rt.connect.states['connect'] == 'Stopped'
    assert rt.connect.states['replacement'] == 'Running'


def test_failed_index_delete_retries_from_retired_tombstone(runtime):
    rt = runtime
    rt.register()
    rt.abort_durable()
    rt.connect.states['connect'] = 'Running'
    rt.now += cleanup.WORKER_WINDOW_SECONDS + 1
    failed = [False]
    def fail_delete(operation, kwargs):
        if operation == 'delete' and not failed[0]:
            failed[0] = True
            raise RuntimeError('temporary index delete failure')
    rt.table.before = fail_delete
    cleanup.reap()
    assert rt.intent()['state'] == 'RETIRED' and rt.indexed()
    stop_count = rt.connect.calls.count(('stop', 'connect'))
    cleanup.reap()
    assert not rt.indexed()
    assert rt.connect.calls.count(('stop', 'connect')) == stop_count


@pytest.mark.parametrize('bootstrap', ['missing_heartbeat', 'stale_heartbeat'])
def test_repeated_reaper_bootstrap_failure_does_not_consume_start_attempts(runtime, bootstrap):
    rt = runtime
    control_key = (cleanup._ACTIVE_PK, cleanup._CONTROL_SK)
    if bootstrap == 'missing_heartbeat':
        rt.table.rows.pop(control_key)
    else:
        rt.table.rows[control_key]['heartbeatAt'] = rt.now - 181
    for _ in range(5):
        run = rt.gate()
        rt.save_run(run)  # The real lifecycle caller persists the helper's outcome.
        assert rt.cs()['status'] == 'running'
        assert not rt.cs().get('precallVoiceStartAttempts')
    assert ('start', 'connect') not in rt.connect.calls
    assert cleanup.reap()['ok'] is True  # A healthy independent pass bootstraps readiness.
    rt.gate()
    assert rt.connect.calls.count(('start', 'connect')) == 1
    assert rt.cs()['precallVoiceStartAttempts'] == 1


def test_failed_final_window_check_rolls_back_unexecuted_start_attempt(runtime):
    rt = runtime
    with patch.object(cleanup, 'check_start_window', side_effect=[
        None, cleanup.CleanupUnavailable('heartbeat failed before Start'),
    ]):
        run = rt.gate()
    rt.save_run(run)
    assert rt.intent() and rt.indexed()
    assert not rt.cs().get('precallVoiceStartAttempts')
    assert ('start', 'connect') not in rt.connect.calls
    rt.gate()
    assert rt.connect.calls.count(('start', 'connect')) == 1
    assert rt.cs()['precallVoiceStartAttempts'] == 1


def test_zero_budget_with_backlog_cannot_refresh_reaper_readiness(runtime):
    rt = runtime
    rt.register()
    old_heartbeat = rt.now
    rt.now += 181
    context = MagicMock()
    context.get_remaining_time_in_millis.return_value = 40000
    result = cleanup.reap(context)
    assert result == {'ok': True, 'processed': 0, 'reason': 'insufficient_budget'}
    control = rt.table.rows[(cleanup._ACTIVE_PK, cleanup._CONTROL_SK)]
    assert control['heartbeatAt'] == old_heartbeat
    assert not control.get('cursor')
    assert 'leaseToken' not in control
    assert rt.indexed()
    with pytest.raises(cleanup.CleanupUnavailable, match='heartbeat_stale'):
        rt.register()


def test_failed_stop_marks_degraded_even_when_previous_heartbeat_is_fresh(runtime):
    rt = runtime
    rt.register()
    rt.abort_durable()
    rt.connect.states['connect'] = 'Running'
    def stop_failure(_):
        raise RuntimeError('temporary stop failure')
    rt.connect.before['stop'] = stop_failure
    rt.now += 50
    result = cleanup.reap()
    assert result['ok'] is False and result['counts']['failed'] == 1
    control = rt.table.rows[(cleanup._ACTIVE_PK, cleanup._CONTROL_SK)]
    assert rt.now - control['heartbeatAt'] == 50
    assert control['degraded'] is True
    with pytest.raises(cleanup.CleanupUnavailable, match='degraded'):
        rt.register()
    assert rt.indexed()


def test_healthy_later_page_cannot_reopen_until_whole_cycle_is_healthy(runtime, monkeypatch):
    rt = runtime
    monkeypatch.setattr(cleanup, '_PAGE_SIZE', 1)
    for n in range(3):
        run, _, cs = profile_model(precallSmsState='complete')
        run['runId'] = f'r{n}'
        cs['connectCampaignId'] = f'connect{n}'
        rt.put_run(run)
        cleanup.register_start(run, cs, now=rt.now)
        rt.connect.states[cs['connectCampaignId']] = 'Running'
        if n == 0:
            run['status'] = 'aborted'
            cs['status'] = 'cancelled'
            rt.put_run(run)
    def stop_failure(_):
        raise RuntimeError('first page Stop unavailable')
    rt.connect.before['stop'] = stop_failure
    old_heartbeat = rt.now
    rt.now += 50
    assert cleanup.reap()['counts'] == {'failed': 1}
    for _ in range(2):
        assert cleanup.reap()['counts'] == {'active': 1}
        with pytest.raises(cleanup.CleanupUnavailable, match='degraded'):
            rt.register()
    control = rt.table.rows[(cleanup._ACTIVE_PK, cleanup._CONTROL_SK)]
    assert control['cursor'] == {} and control['cycleFailed'] is False
    assert control['degraded'] is True and control['heartbeatAt'] == old_heartbeat
    rt.connect.before.pop('stop')
    for _ in range(2):
        cleanup.reap()
        with pytest.raises(cleanup.CleanupUnavailable, match='degraded'):
            rt.register()
    cleanup.reap()
    control = rt.table.rows[(cleanup._ACTIVE_PK, cleanup._CONTROL_SK)]
    assert control['degraded'] is False and control['heartbeatAt'] == rt.now
    rt.register()
    assert rt.connect.states['connect0'] == 'Stopped'
