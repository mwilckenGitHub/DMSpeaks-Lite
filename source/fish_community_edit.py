"""Durable local bridge for the pinned community Fish native-edit adapter.

The codec worker runs in its own installed Python environment. This module has
no torch dependency. It never retries an ambiguous POST: the upstream speech
route does not promise idempotency. Returned inserts still require review.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from fish_community_tts import _validate_endpoint, check
from speech_edit import plan_edits
from speech_edit_client import PendingSpeechEdit

CAPABILITIES = {
    'contextual_infilling': True, 'exact_duration': False,
    'codec_sample_rate': 44100, 'codec_frame_samples': 2048,
    'context_encodings': ['global_codec'],
    'adapter': 'dmspeaks.community_fish_edit.1',
    'output_requires_isolation_review': True,
}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_config(path):
    config = json.loads(Path(path).read_text())
    if config.get('schema') != 'dmspeaks.community_edit_runtime.1':
        raise ValueError('Install a verified community native-edit runtime configuration.')
    _validate_endpoint(config['endpoint'])
    for key in ('python', 'codec_checkpoint', 'capture_directory'):
        if not Path(config[key]).is_absolute() or not Path(config[key]).exists():
            raise ValueError('Missing community runtime path: ' + key)
    fingerprints = config.get('runtime_files', {})
    if not fingerprints:
        raise ValueError('Community adapter source fingerprints are missing.')
    for filename, expected in fingerprints.items():
        if digest(filename) != expected:
            raise ValueError('Community adapter changed; verify it before editing: ' + filename)
    return config


def batch_identity(request, config, seed):
    if type(seed) is not int:
        raise ValueError('Seed must be an integer.')
    jobs = plan_edits(request, CAPABILITIES)
    if request['policy'].get('context_encoding', 'global_codec') != 'global_codec':
        raise ValueError('This bridge uses one global source codec encoding.')
    source = request['source']
    if source['sample_rate'] != 44100:
        raise ValueError('Community native editing requires a 44,100 Hz source.')
    if not source.get('sha256') or digest(source['audio_uri']) != source['sha256']:
        raise ValueError('The source recording checksum changed or is missing.')
    identity = {'request': request, 'runtime': config, 'seed': seed,
                'bridge_sha256': digest(__file__),
                'codec_worker_sha256': digest(Path(__file__).with_name('fish_community_codec.py'))}
    signature = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                          separators=(',', ':')).encode()).hexdigest()
    return signature, jobs


def run_batch(request, config, state_path, seed, engine):
    """Single writer. Engine is injectable for offline crash/recovery tests.

    response_saved resumes local decoding only; submitted without a durable
    response is ambiguous and is never submitted again automatically.
    """
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        signature, jobs = batch_identity(request, config, seed)
        if path.exists():
            state = json.loads(path.read_text())
            if state.get('signature') != signature:
                raise ValueError('Saved edit batch belongs to different inputs or runtime.')
            if [(j['slot_id'], j['value_id']) for j in state['jobs']] != [(j['slot_id'], j['value_id']) for j in jobs]:
                raise ValueError('Saved batch candidates no longer match its identity.')
        else:
            state = {'schema': 'dmspeaks.community_edit_batch.1', 'signature': signature,
                     'endpoint': config['endpoint'], 'status': 'running', 'jobs': [
                         {'request_id': 'edit-' + uuid.uuid4().hex,
                          'slot_id': job['slot_id'], 'value_id': job['value_id'],
                          'status': 'not_submitted'} for job in jobs]}
        state.update(status='running', worker_pid=os.getpid())
        atomic_json(path, state)
        artifact_root = path.with_suffix('.artifacts')
        artifact_root.mkdir(exist_ok=True)
        try:
            for item, job in zip(state['jobs'], jobs):
                if item['status'] == 'complete':
                    result = item['result']
                    if digest(result['output']) != result['output_sha256']:
                        raise ValueError('Retained infill audio changed; refusing cached output.')
                    continue
                if item['status'] in ('failed', 'ambiguous'):
                    continue
                folder = artifact_root / item['request_id']
                folder.mkdir(exist_ok=True)
                if item['status'] == 'submitted':
                    # The network reply may have arrived just before a crash.
                    receipt = folder / 'receipt.json'
                    if receipt.is_file():
                        item['response'] = json.loads(receipt.read_text())
                        item['status'] = 'response_saved'
                    else:
                        item.update(status='ambiguous', error='The prior request has no saved response. It was not resubmitted; inspect the runtime before creating another batch.')
                        atomic_json(path, state)
                        continue
                try:
                    if item['status'] == 'not_submitted':
                        payload = engine.prepare(job, folder, seed)
                        atomic_json(folder / 'request.json', payload)
                        # Persist before network I/O; no idempotency assumption.
                        item['status'] = 'submitted'
                        atomic_json(path, state)
                        receipt = engine.submit(payload, folder)
                        atomic_json(folder / 'receipt.json', receipt)
                        item.update(status='response_saved', response=receipt)
                        atomic_json(path, state)
                    result = engine.finish(job, folder, item['response'])
                    result.update(request_id=item['request_id'], slot_id=item['slot_id'],
                                  value_id=item['value_id'], source_sha256=request['source']['sha256'],
                                  output_sha256=digest(result['output']),
                                  context_encoding='global_codec', delivery_approved=False,
                                  isolation_review_required=True)
                    item.update(status='complete', result=result)
                except Exception as error:
                    if item['status'] == 'submitted':
                        item.update(status='ambiguous', error=str(error) + ' No automatic resubmission.')
                    elif item['status'] == 'response_saved':
                        # A decoder failure can resume from this exact receipt.
                        item['error'] = str(error)
                        atomic_json(path, state)
                        raise
                    else:
                        item.update(status='failed', error=str(error))
                atomic_json(path, state)
            state['status'] = 'complete'
        except Exception as error:
            state.update(status='interrupted', error=str(error))
            raise
        finally:
            atomic_json(path, state)
        return copy.deepcopy(state)


class CommunitySpeechEditClient:
    def __init__(self, endpoint, config_path):
        self.config_path = Path(config_path)
        self.config = read_config(self.config_path)
        if _validate_endpoint(endpoint) != _validate_endpoint(self.config['endpoint']):
            raise ValueError('Selected connection differs from the installed native-edit runtime.')

    def capabilities(self):
        if not self.config.get('enabled'):
            raise ValueError('Community native-edit bridge is not enabled for this installation. No fallback was selected.')
        if not check(self.config['endpoint']):
            raise ValueError('The configured community Fish runtime is not ready. No fallback was selected.')
        return copy.deepcopy(CAPABILITIES)

    def batch(self, request, state_path, seed=42, timeout_per_job=1800, on_status=None):
        self.capabilities()
        path = Path(state_path)
        signature, jobs = batch_identity(request, self.config, seed)
        path.parent.mkdir(parents=True, exist_ok=True)
        envelope = path.with_suffix('.input.json')
        # Same batch input is written under a separate lock before worker launch.
        with envelope.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = {'request': request, 'config': self.config, 'seed': seed, 'signature': signature}
            if envelope.exists() and json.loads(envelope.read_text()) != data:
                raise ValueError('Saved edit batch input differs. Build a new plan.')
            if not envelope.exists():
                atomic_json(envelope, data)
        command = [self.config['python'], str(Path(__file__).resolve()), '--worker', str(envelope), str(path)]
        with path.with_suffix('.worker.log').open('ab') as log:
            process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + timeout_per_job * max(1, len(jobs))
        last = None
        while process.poll() is None:
            if path.exists():
                state = json.loads(path.read_text())
                active = next((i for i in state['jobs'] if i['status'] not in ('complete', 'failed', 'ambiguous')), None)
                if active and on_status:
                    current = (active['request_id'], active['status'])
                    if current != last:
                        on_status(*current)
                        last = current
            if time.monotonic() >= deadline:
                # Leave the worker and durable files intact for resume.
                raise PendingSpeechEdit(path.stem)
            time.sleep(.5)
        if process.returncode == 75:
            raise PendingSpeechEdit(path.stem)
        if not path.exists():
            raise ValueError('Community codec worker failed before saving a batch. See ' + str(path.with_suffix('.worker.log')))
        state = json.loads(path.read_text())
        if state.get('signature') != signature:
            raise ValueError('Community worker returned a different batch identity.')
        if process.returncode or state['status'] != 'complete':
            raise ValueError(state.get('error', 'Community edit worker interrupted; retained response can be resumed.'))
        return state


if __name__ == '__main__':
    if len(sys.argv) != 4 or sys.argv[1] != '--worker':
        raise SystemExit('Use the DMSpeaks community edit client.')
    from fish_community_codec import CommunityCodecEngine
    envelope = json.loads(Path(sys.argv[2]).read_text())
    config = envelope['config']
    engine = CommunityCodecEngine(config)
    result = run_batch(envelope['request'], config, sys.argv[3], envelope['seed'], engine)
    raise SystemExit(75 if result is None else 0)
