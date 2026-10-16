import copy
import json
from pathlib import Path
import tempfile
import unittest

import edge_deploy as d

SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
CONFIG = {
    'owner': 'fespadoni',
    'components': {
        'web': {'container': 'contratos-web-1', 'repository': 'fespadoni/contratos-web', 'user': '1000'},
        'worker': {'container': 'contratos-worker-1', 'repository': 'fespadoni/contratos-worker', 'health': 'running'},
    },
    'keep_identity': ['contratos-db-1'],
    'checks': [{'url': 'http://127.0.0.1:18080/health', 'status': 200}],
}


def snapshot(name, container):
    return {'Id': 'old-' + name, 'Name': '/' + container, 'Image': 'old-image-' + name,
            'Config': {'User': '1000', 'Env': ['SECRET=synthetic'], 'Labels': {}, 'Cmd': ['app'],
                       'StopSignal': 'SIGQUIT',
                       'Healthcheck': {'Test': ['CMD', 'true'], 'Interval': 30 * 10**9, 'StartPeriod': 5 * 10**9}},
            'HostConfig': {'Privileged': False, 'Binds': ['/srv/contratos/data:/data'], 'Memory': 256 * 1024**2,
                           'NetworkMode': 'contratos_default', 'PortBindings': {'8080/tcp': [{'HostPort': '18080'}]}},
            'NetworkSettings': {'Networks': {'contratos_default': {'Aliases': [name, 'old-' + name]},
                                             'edge': {'Aliases': [name]}}},
            'State': {'Running': True, 'Health': {'Status': 'healthy'}}}


class FakeEngine:
    def __init__(self, fail_start=False, image_sha=SHA, db_changes=False):
        self.containers = {}
        for name, spec in CONFIG['components'].items():
            snap = snapshot(name, spec['container'])
            self.containers[snap['Id']] = snap
        self.containers['db'] = {'Id': 'db', 'Name': '/contratos-db-1', 'Image': 'postgres',
                                 'Config': {}, 'HostConfig': {}, 'NetworkSettings': {'Networks': {}},
                                 'State': {'Running': True}}
        self.fail_start, self.image_sha, self.db_changes = fail_start, image_sha, db_changes
        self.operations, self.payloads, self.counter = [], {}, 0

    def inspect(self, identifier):
        found = self.containers.get(identifier) or next(
            (c for c in self.containers.values() if c['Name'] == '/' + identifier), None)
        if found is None:
            raise d.DeployError('Container not found')
        if identifier == 'contratos-db-1' and self.db_changes and any(o[0] == 'create' for o in self.operations):
            return dict(found, Id='db-replaced')
        return copy.deepcopy(found)

    def image(self, tag):
        name = tag.split('/')[1].split(':')[0].removeprefix('contratos-')
        return {'Id': 'new-image-' + name, 'Config': {'User': '1000', 'Labels': {d.REVISION: self.image_sha}}}

    def action(self, identifier, action, query=None):
        self.operations.append((action, identifier))
        container = self.containers[identifier] if identifier in self.containers else self.inspect(identifier)
        if action == 'rename':
            self.containers[container['Id']]['Name'] = '/' + query['name']
        if action == 'stop':
            self.containers[container['Id']]['State']['Running'] = False
        if action == 'start':
            if self.fail_start and container['Id'].startswith('new-'):
                raise d.DeployError('Synthetic start failure')
            self.containers[container['Id']]['State']['Running'] = True

    def create(self, name, payload):
        self.counter += 1
        identifier = f'new-{self.counter}'
        self.operations.append(('create', name))
        self.payloads[name] = payload
        self.containers[identifier] = {'Id': identifier, 'Name': '/' + name, 'Image': payload['Image'],
                                       'Config': {'Labels': payload['Labels']}, 'HostConfig': payload['HostConfig'],
                                       'NetworkSettings': {'Networks': payload['NetworkingConfig']['EndpointsConfig']},
                                       'State': {'Running': False, 'Health': {'Status': 'healthy'}}}
        return identifier

    def remove(self, identifier):
        self.operations.append(('remove', identifier))
        self.containers.pop(identifier, None)

    def connect(self, network, identifier, settings):
        self.containers[identifier]['NetworkSettings']['Networks'][network] = settings


def always(_engine, _config):
    return True


class Deploy(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp()) / 'contratos'

    def test_success_replaces_every_component_and_keeps_its_runtime(self):
        engine = FakeEngine()
        result = d.deploy(engine, 'contratos', SHA, CONFIG, self.directory, health=always)
        self.assertEqual(result['changed'], ['web', 'worker'])
        web = engine.payloads['contratos-web-1']
        self.assertEqual(web['Image'], 'new-image-web')
        self.assertEqual(web['Env'], ['SECRET=synthetic'])
        self.assertEqual(web['HostConfig']['Binds'], ['/srv/contratos/data:/data'])
        self.assertEqual(set(web['NetworkingConfig']['EndpointsConfig']), {'contratos_default', 'edge'})
        self.assertNotIn('old-web', web['NetworkingConfig']['EndpointsConfig']['contratos_default']['Aliases'])
        self.assertNotIn('StopSignal', web)
        self.assertEqual(web['Healthcheck']['StartInterval'], 2 * 10**9)
        self.assertEqual(web['Labels'][d.REVISION], SHA)
        self.assertEqual(json.loads((self.directory / 'current.json').read_text())['sha'], SHA)
        self.assertNotIn('old-web', engine.containers)  # the previous container is removed after success

    def test_failed_health_restores_the_previous_containers(self):
        engine = FakeEngine()
        with self.assertRaisesRegex(d.DeployError, 'rollback healthy=false'):
            d.deploy(engine, 'contratos', SHA, CONFIG, self.directory, health=lambda e, c: False)
        for name, spec in CONFIG['components'].items():
            original = engine.containers['old-' + name]
            self.assertEqual(original['Name'], '/' + spec['container'])
            self.assertTrue(original['State']['Running'])
        self.assertFalse(any(i.startswith('new-') for i in engine.containers))
        self.assertFalse((self.directory / 'current.json').exists())

    def test_a_start_failure_rolls_back(self):
        engine = FakeEngine(fail_start=True)
        with self.assertRaisesRegex(d.DeployError, 'Deployment failed'):
            d.deploy(engine, 'contratos', SHA, CONFIG, self.directory, health=always)
        self.assertEqual(engine.containers['old-web']['Name'], '/contratos-web-1')

    def test_a_changed_protected_container_rolls_back(self):
        engine = FakeEngine(db_changes=True)
        with self.assertRaisesRegex(d.DeployError, 'Deployment failed'):
            d.deploy(engine, 'contratos', SHA, CONFIG, self.directory, health=always)

    def test_wrong_revision_stops_before_touching_anything(self):
        engine = FakeEngine(image_sha='c' * 40)
        with self.assertRaisesRegex(d.DeployError, 'revision'):
            d.deploy(engine, 'contratos', SHA, CONFIG, self.directory, health=always)
        self.assertEqual(engine.operations, [])

    def test_a_docker_socket_mount_is_refused_before_touching_anything(self):
        engine = FakeEngine()
        engine.containers['old-web']['HostConfig']['Binds'].append('/var/run/docker.sock:/var/run/docker.sock')
        with self.assertRaisesRegex(d.DeployError, 'Docker socket'):
            d.deploy(engine, 'contratos', SHA, CONFIG, self.directory, health=always)
        self.assertEqual(engine.operations, [])


class Command(unittest.TestCase):
    def setUp(self):
        self.config_dir = Path(tempfile.mkdtemp())
        (self.config_dir / 'contratos.json').write_text(json.dumps(CONFIG))

    def command(self, *parts):
        return ' '.join(('deploy',) + parts)

    def test_accepts_exactly_one_digest_per_component(self):
        app, sha, digests, config = d.parse_command(
            self.command('contratos', SHA, 'web=' + DIGEST, 'worker=' + DIGEST), 'fespadoni', self.config_dir, False)
        self.assertEqual((app, sha, digests), ('contratos', SHA, {'web': DIGEST, 'worker': DIGEST}))

    def test_refuses_another_owner_missing_components_and_bad_input(self):
        cases = [
            (self.command('contratos', SHA, 'web=' + DIGEST, 'worker=' + DIGEST), 'renecaldeira'),
            (self.command('contratos', SHA, 'web=' + DIGEST), 'fespadoni'),
            (self.command('contratos', SHA, 'web=' + DIGEST, 'worker=' + DIGEST, 'extra=' + DIGEST), 'fespadoni'),
            (self.command('contratos', SHA[:-1], 'web=' + DIGEST, 'worker=' + DIGEST), 'fespadoni'),
            (self.command('../etc', SHA, 'web=' + DIGEST), 'fespadoni'),
            (self.command('missing', SHA, 'web=' + DIGEST), 'fespadoni'),
            (self.command('contratos', SHA, 'web=md5:abc', 'worker=' + DIGEST), 'fespadoni'),
        ]
        for command, owner in cases:
            with self.subTest(command=command, owner=owner), self.assertRaises(d.DeployError):
                d.parse_command(command, owner, self.config_dir, False)

    def test_a_check_may_only_target_the_local_host(self):
        bad = dict(CONFIG, checks=[{'url': 'http://example.com/health'}])
        (self.config_dir / 'contratos.json').write_text(json.dumps(bad))
        with self.assertRaisesRegex(d.DeployError, '127.0.0.1'):
            d.load_config('contratos', 'fespadoni', self.config_dir, False)

    def test_pull_is_by_digest_from_the_local_registry(self):
        calls = []
        d.pull('contratos', SHA, {'web': DIGEST}, CONFIG, run=lambda argv, **_: calls.append(argv))
        self.assertEqual(calls, [
            ['docker', 'pull', '--quiet', '127.0.0.1:5000/fespadoni/contratos-web@' + DIGEST],
            ['docker', 'tag', '127.0.0.1:5000/fespadoni/contratos-web@' + DIGEST, d.image_tag('contratos', 'web', SHA)],
        ])


if __name__ == '__main__':
    unittest.main()
