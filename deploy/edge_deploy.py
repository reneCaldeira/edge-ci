#!/usr/bin/env python3
"""Generic deployment helper for apps on the Edge VPS: replaces an app's containers with images from the local
registry and rolls back on any failure.

Runs as root through one restricted SSH account. Each owner's runner user has its own key, whose forced command
fixes the owner:   command="sudo -n /usr/local/sbin/edge-app-deploy <owner>"
The CI sends:     deploy <app> <40-hex commit> <component>=sha256:<64 hex> [...]
so a job can only deploy apps whose configuration names its owner, and only the exact image contents (digests)
it built and scanned. Every app is described by /etc/edge-app-deploy/apps/<app>.json (root-owned, see README).

The containers keep their whole runtime configuration (environment, mounts, ports, networks, limits); only the
image changes. Before anything stops, every container and image is checked; afterwards the app must turn healthy
within the timeout, or every replaced container is restored.
"""
import copy
import fcntl
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import urlopen
import uuid

CONFIG_DIR = Path('/etc/edge-app-deploy/apps')
STATE_DIR = Path('/var/lib/edge-app-deploy')
REGISTRY = '127.0.0.1:5000'
REVISION = 'org.opencontainers.image.revision'
TRANSACTION = 'io.edge.deploy-transaction'
MAX_JSON = 1024**2
NAME = re.compile(r'[a-z0-9][a-z0-9_.-]{0,62}')


class DeployError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise DeployError(message)


# ---------- configuration ------------------------------------------------------------------------------------

def load_config(app, owner, config_dir=CONFIG_DIR, root_owned=True):
    require(NAME.fullmatch(app or ''), 'Invalid app name')
    path = config_dir / f'{app}.json'
    require(path.is_file(), 'Unknown app')
    info = path.stat()
    if root_owned:
        require(info.st_uid == 0 and not stat.S_IMODE(info.st_mode) & 0o022, 'App configuration must be root-owned')
    config = json.loads(path.read_text(encoding='utf-8'))
    require(config.get('owner') == owner, 'This owner may not deploy this app')
    components = config.get('components')
    require(isinstance(components, dict) and components, 'App has no components')
    for name, component in components.items():
        require(NAME.fullmatch(name), 'Invalid component name')
        require(isinstance(component.get('container'), str) and component['container'], 'Component without container')
        require(re.fullmatch(r'[a-z0-9][a-z0-9_./-]*', component.get('repository', '')), 'Invalid registry repository')
        require(component.get('health', 'docker') in ('docker', 'running'), 'Invalid health mode')
    for check in config.get('checks', []):
        require(re.fullmatch(r'http://127\.0\.0\.1:\d+/\S*', check.get('url', '')), 'Checks must target 127.0.0.1')
        require(isinstance(check.get('status', 200), int), 'Invalid check status')
    return config


def parse_command(command, owner, config_dir=CONFIG_DIR, root_owned=True):
    match = re.fullmatch(r'deploy ([a-z0-9][a-z0-9_.-]{0,62}) ([0-9a-f]{40})((?: [a-z0-9][a-z0-9_.-]{0,62}=sha256:[0-9a-f]{64})+)',
                         command or '')
    require(match, 'Invalid deployment command')
    app, sha = match.group(1), match.group(2)
    config = load_config(app, owner, config_dir, root_owned)
    digests = dict(item.split('=', 1) for item in match.group(3).split())
    require(set(digests) == set(config['components']), 'Every component needs exactly one digest')
    return app, sha, digests, config


# ---------- Docker ---------------------------------------------------------------------------------------------

class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        endpoint = os.environ.get('DOCKER_HOST', 'unix:///var/run/docker.sock') if os.geteuid() != 0 else 'unix:///var/run/docker.sock'
        require(endpoint.startswith('unix:///'), 'Docker endpoint must be a local Unix socket')
        self.sock.connect(endpoint[len('unix://'):])


class Engine:
    def __init__(self):
        version = self.request('GET', '/version', versioned=False).get('ApiVersion', '')
        require(re.fullmatch(r'1\.\d+', version) and int(version.split('.')[1]) >= 47, 'Unsupported Docker API')
        self.version = '/v' + version

    def request(self, method, path, body=None, *, versioned=True, allowed=()):
        # a stop alone may take its full timeout and a loaded host answers slowly
        connection = UnixConnection('localhost', timeout=120)
        try:
            payload = json.dumps(body).encode() if body is not None else None
            connection.request(method, (self.version if versioned else '') + path, payload,
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            data = response.read(8 * MAX_JSON + 1)
            require(len(data) <= 8 * MAX_JSON, 'Docker response exceeds limit')
            require(200 <= response.status < 300 or response.status in allowed, 'Docker operation failed')
            return json.loads(data) if data and 200 <= response.status < 300 else None
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise DeployError('Docker communication failed') from error
        finally:
            connection.close()

    def inspect(self, identifier):
        return self.request('GET', '/containers/' + quote(identifier, safe='') + '/json')

    def image(self, identifier):
        return self.request('GET', '/images/' + quote(identifier, safe='') + '/json')

    def action(self, identifier, action, query=None):
        suffix = '?' + urlencode(query) if query else ''
        return self.request('POST', '/containers/' + quote(identifier, safe='') + '/' + action + suffix,
                            allowed=(304,) if action in ('start', 'stop') else ())

    def remove(self, identifier):
        self.request('DELETE', '/containers/' + quote(identifier, safe='') + '?force=true&v=false')

    def create(self, name, payload):
        return self.request('POST', '/containers/create?' + urlencode({'name': name}), payload)['Id']

    def connect(self, network, identifier, settings):
        self.request('POST', '/networks/' + quote(network, safe='') + '/connect',
                     {'Container': identifier, 'EndpointConfig': settings})


def image_tag(app, component, sha):
    return f'edge-app-deploy/{app}-{component}:{sha}'


def pull(app, sha, digests, config, run=subprocess.run):
    for component, digest in digests.items():
        source = f"{REGISTRY}/{config['components'][component]['repository']}@{digest}"
        run(['docker', 'pull', '--quiet', source], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
        run(['docker', 'tag', source, image_tag(app, component, sha)], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


# ---------- validation -----------------------------------------------------------------------------------------

def validate_runtime(snapshot, component):
    require(snapshot['Name'] == '/' + component['container'], 'Container identity mismatch')
    host = snapshot['HostConfig']
    require(not host.get('Privileged') and not host.get('CapAdd'), 'Privileged containers are not deployed by this helper')
    require(host.get('NetworkMode') != 'host' and not str(host.get('NetworkMode', '')).startswith('container:'),
            'Unexpected network mode')
    sources = [b.split(':', 1)[0] for b in host.get('Binds') or []]
    sources += [m.get('Source', '') for m in host.get('Mounts') or [] if isinstance(m, dict)]
    require(not any(s.rstrip('/').endswith('docker.sock') for s in sources), 'A Docker socket mount is never deployed')


def validate_image(config, component, sha):
    require(isinstance(config, dict), 'Invalid image configuration')
    require(config.get('Labels', {}).get(REVISION) == sha, 'Image revision mismatch')
    if component.get('user') is not None:
        require(config.get('User') == component['user'], 'Unexpected image runtime user')


def endpoint_settings(snapshot):
    old_id = snapshot['Id']
    return {name: {'Aliases': [a for a in settings.get('Aliases') or []
                               if isinstance(a, str) and a not in (old_id, old_id[:12])]}
            for name, settings in snapshot['NetworkSettings']['Networks'].items()}


def create_payload(snapshot, image_id, sha, transaction=None):
    config = snapshot['Config']
    # StopSignal is not copied: the new image's own signal applies.
    keys = ('Env', 'User', 'WorkingDir', 'Entrypoint', 'Cmd', 'Labels', 'ExposedPorts', 'Healthcheck',
            'StopTimeout', 'Tty', 'OpenStdin', 'StdinOnce')
    payload = {k: copy.deepcopy(config[k]) for k in keys if k in config}
    check = payload.get('Healthcheck')
    if check and check.get('Test') and check['Test'] != ['NONE']:
        # probe every 2 s while the container starts instead of waiting a whole interval
        check['StartInterval'] = 2 * 10**9
        check['StartPeriod'] = max(check.get('StartPeriod') or 0, 60 * 10**9)
    payload.update(Image=image_id, Hostname=snapshot['Config'].get('Hostname') or snapshot['Name'].lstrip('/'))
    payload['Labels'] = dict(payload.get('Labels') or {}, **{REVISION: sha})
    if transaction:
        payload['Labels'][TRANSACTION] = transaction
    payload['HostConfig'] = copy.deepcopy(snapshot['HostConfig'])
    networks = endpoint_settings(snapshot)
    mode = snapshot['HostConfig'].get('NetworkMode')
    # the original may name its network by id; Docker API >= 1.44 takes every endpoint at creation
    payload['HostConfig']['NetworkMode'] = mode if mode in networks else (sorted(networks)[0] if networks else 'bridge')
    payload['NetworkingConfig'] = {'EndpointsConfig': networks}
    return payload


def status_code(url):
    try:
        with urlopen(url, timeout=3) as response:
            return response.status
    except HTTPError as error:
        return error.code
    except (OSError, URLError):
        return None


def healthy(engine, config):
    for component in config['components'].values():
        state = engine.inspect(component['container'])['State']
        if not state.get('Running'):
            return False
        if component.get('health', 'docker') == 'docker' and state.get('Health') \
                and state['Health'].get('Status') != 'healthy':
            return False
    return all(status_code(c['url']) == c.get('status', 200) for c in config.get('checks', []))


def wait_healthy(engine, config):
    deadline = time.monotonic() + int(config.get('health_timeout', 180))
    while time.monotonic() < deadline:
        if healthy(engine, config):
            return True
        time.sleep(2)
    return False


def identities(engine, config):
    return {name: engine.inspect(name)['Id'] for name in config.get('keep_identity', [])}


# ---------- deployment -----------------------------------------------------------------------------------------

def write_state(directory, state):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = directory / 'current.json.new'
    temporary.write_text(json.dumps(state) + '\n', encoding='utf-8')
    temporary.chmod(0o600)
    temporary.replace(directory / 'current.json')


def deploy(engine, app, sha, config, directory, health=wait_healthy):
    components = config['components']
    originals = {c: engine.inspect(spec['container']) for c, spec in components.items()}
    kept = identities(engine, config)
    images = {}
    for name, spec in components.items():
        validate_runtime(originals[name], spec)
        image = engine.image(image_tag(app, name, sha))
        validate_image(image['Config'], spec, sha)
        images[name] = image['Id']
    changed = [c for c in components if originals[c]['Image'] != images[c]]
    touched, created = [], {}
    transaction = uuid.uuid4().hex
    try:
        for name in changed:
            snapshot, container = originals[name], components[name]['container']
            touched.append(name)
            engine.action(snapshot['Id'], 'stop', {'t': 15})
            engine.action(snapshot['Id'], 'rename', {'name': container + '-previous-' + transaction[:12]})
            created[name] = engine.create(container, create_payload(snapshot, images[name], sha, transaction))
            engine.action(created[name], 'start')
        require(health(engine, config), 'New release health check failed')
        require(identities(engine, config) == kept, 'A protected container changed')
        write_state(directory, {'sha': sha, 'images': images, 'previous': {c: s['Image'] for c, s in originals.items()}})
    except Exception:
        restored = True
        for name in reversed(touched):
            snapshot, container = originals[name], components[name]['container']
            try:
                if name in created:
                    engine.remove(created[name])
                try:
                    occupant = engine.inspect(container)
                except DeployError:
                    occupant = None
                if occupant and occupant['Id'] != snapshot['Id']:
                    # only a container this transaction created may be removed from the canonical name
                    require(occupant['Image'] == images[name]
                            and occupant['Config'].get('Labels', {}).get(TRANSACTION) == transaction,
                            'Unexpected rollback name occupant')
                    engine.remove(occupant['Id'])
                current = engine.inspect(snapshot['Id'])
                if current['Name'] != snapshot['Name']:
                    engine.action(snapshot['Id'], 'rename', {'name': container})
                for network in set(endpoint_settings(snapshot)) - set(current['NetworkSettings']['Networks']):
                    engine.connect(network, snapshot['Id'], endpoint_settings(snapshot)[network])
                engine.action(snapshot['Id'], 'start')
            except Exception:
                restored = False
        try:
            restored = bool(health(engine, config)) and restored and identities(engine, config) == kept
        except Exception:
            restored = False
        raise DeployError('Deployment failed; rollback healthy=' + str(restored).lower()) from None

    cleanup_ok = True
    for name in changed:
        try:
            engine.remove(originals[name]['Id'])
        except Exception:
            cleanup_ok = False
    return {'app': app, 'sha': sha, 'healthy': True, 'changed': changed, 'backup_cleanup': cleanup_ok}


def main():
    require(os.geteuid() == 0, 'Root helper required')
    require(len(sys.argv) == 2 and re.fullmatch(r'[a-z0-9-]{1,39}', sys.argv[1]), 'Owner must be fixed by the forced command')
    owner = sys.argv[1]
    app, sha, digests, config = parse_command(os.environ.get('SSH_ORIGINAL_COMMAND'), owner)

    def interrupted(_number, _frame):
        raise DeployError('Deployment interrupted')
    for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(number, interrupted)
    os.umask(0o077)
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(f'/run/lock/edge-app-deploy-{app}.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        require(os.fstat(lock.fileno()).st_uid == 0, 'Unsafe deployment lock')
        fcntl.flock(lock, fcntl.LOCK_EX)
        pull(app, sha, digests, config)
        print(json.dumps(deploy(Engine(), app, sha, config, STATE_DIR / app)))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error) if isinstance(error, DeployError) else 'Deployment failed', file=sys.stderr)
        sys.exit(1)
