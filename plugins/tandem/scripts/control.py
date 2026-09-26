"""Reversible desktop configuration and bridge lifecycle."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request

from cryptography.fernet import Fernet
from bridge import CODEX_HOME, PORT, ROOT, STATE
from harness import HARNESS_MODEL as MODEL

CONFIG = CODEX_HOME / 'config.toml'
CATALOG = STATE / 'models.json'
ROUTER_PORT = 57856
# Codex desktop ships its own Node.js runtime; fall back to one on PATH.
BUNDLED_NODE = Path.home() / '.cache' / 'codex-runtimes' / 'codex-primary-runtime' / 'dependencies' / 'node' / 'bin' / ('node.exe' if os.name == 'nt' else 'node')


def node():
    found = os.environ.get('TANDEM_NODE') or (str(BUNDLED_NODE) if BUNDLED_NODE.exists() else shutil.which('node'))
    if not found:
        raise RuntimeError('Node.js was not found. Install Node.js 22.15+ or set TANDEM_NODE.')
    return found


def build_catalog():
    """Codex's cached GPT catalog plus the Tandem model, written where Codex will read it."""
    try:
        cached = json.loads((CODEX_HOME / 'models_cache.json').read_text(encoding='utf-8'))['models']
    except (OSError, ValueError, KeyError) as error:
        raise RuntimeError('Codex has not cached its model list yet. Open Codex once, then enable Tandem again.') from error
    ours = json.loads((ROOT / 'models.json').read_text(encoding='utf-8'))['models']
    slugs = {model['slug'] for model in ours}
    STATE.mkdir(parents=True, exist_ok=True)
    temporary = CATALOG.with_suffix('.tmp')
    temporary.write_text(json.dumps({'models': [m for m in cached if m.get('slug') not in slugs] + ours}, indent=2), encoding='utf-8')
    os.replace(temporary, CATALOG)


def refresh_catalog():
    # Keep newly released GPT models visible once Codex has refreshed its cache.
    source = CODEX_HOME / 'models_cache.json'
    if (STATE / 'desktop-restore.json').exists() and source.exists() and (not CATALOG.exists() or source.stat().st_mtime > CATALOG.stat().st_mtime):
        build_catalog()


def settings():
    STATE.mkdir(parents=True, exist_ok=True)
    target = STATE / 'settings.json'
    if not target.exists():
        # Exclusive creation protects two plugin startup clients racing.
        try:
            with target.open('x') as handle:
                json.dump({'secret': secrets.token_urlsafe(32), 'carrier_key': Fernet.generate_key().decode()}, handle)
        except FileExistsError:
            pass
    return json.loads(target.read_text())


def request(route, method='GET', port=PORT):
    secret = settings()['secret']
    req = urllib.request.Request(f'http://127.0.0.1:{port}{route}', headers={'Authorization': 'Bearer ' + secret}, method=method)
    with urllib.request.urlopen(req, timeout=3) as response:
        return json.load(response)


def status():
    try:
        health = request('/health')
        try:
            health['router'] = request('/health', port=ROUTER_PORT)
        except (OSError, urllib.error.URLError):
            health['router'] = {'running': False}
        return health
    except (OSError, urllib.error.URLError):
        return {'bridge': 'tandem', 'running': False, 'model': MODEL}


def start():
    settings()
    try:
        refresh_catalog()
    except RuntimeError:
        pass
    health = status()
    if health.get('pid'):
        start_router()
        return status()
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    process = subprocess.Popen([sys.executable, str(ROOT / 'scripts' / 'bridge.py')], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT, creationflags=flags)
    for _ in range(40):
        health = status()
        if health.get('pid'):
            start_router()
            return status()
        if process.poll() is not None:
            raise RuntimeError('Bridge did not start; port may be occupied')
        time.sleep(.15)
    process.terminate()
    raise RuntimeError('Bridge startup timed out')


def start_router():
    try:
        return request('/health', port=ROUTER_PORT)
    except (OSError, urllib.error.URLError):
        pass
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    process = subprocess.Popen([node(), str(ROOT / 'scripts' / 'router.cjs'), str(STATE)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=ROOT, creationflags=flags)
    for _ in range(40):
        try:
            return request('/health', port=ROUTER_PORT)
        except (OSError, urllib.error.URLError):
            if process.poll() is not None:
                raise RuntimeError('Router could not start; port may be occupied')
            time.sleep(.15)
    process.terminate()
    raise RuntimeError('Router startup timed out')


def stop():
    health = status()
    if health.get('router', {}).get('pid'):
        request('/shutdown', 'POST', port=ROUTER_PORT)
    if health.get('pid'):
        return request('/shutdown', 'POST')
    return {'stopping': False, 'running': False}


def replace_roots(text, values):
    # Parse one complete root statement at a time, including multiline strings/arrays.
    tomllib.loads(text)
    lines, result, pending = text.splitlines(keepends=True), [], dict(values)
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith('['):
            break
        statement = line
        index += 1
        while True:
            try:
                parsed = tomllib.loads(statement)
                break
            except tomllib.TOMLDecodeError:
                if index == len(lines):
                    raise
                statement += lines[index]
                index += 1
        key = next(iter(parsed), None)
        if key in pending:
            literal = pending.pop(key)
            if literal is not None:
                result.append(f'{key} = {literal}\n')
        else:
            result.append(statement)
    root = ''.join(result).rstrip('\r\n')
    additions = ''.join(f'{key} = {literal}\n' for key, literal in pending.items() if literal is not None)
    if additions:
        root = (root + '\n' if root else '') + additions
    tables = ''.join(lines[index:])
    return root.rstrip('\r\n') + ('\n\n' + tables if tables else '\n')


def activation_values():
    return {'openai_base_url': json.dumps(f'http://127.0.0.1:{ROUTER_PORT}/' + settings()['secret'] + '/v1'), 'model_catalog_json': json.dumps(str(CATALOG))}


def enable():
    start()
    text = CONFIG.read_text(encoding='utf-8') if CONFIG.exists() else ''
    state_file = STATE / 'desktop-restore.json'
    current = tomllib.loads(text)
    if state_file.exists() and current.get('model_catalog_json') == str(CATALOG):
        return {'enabled': True, 'added_model': MODEL, 'default_model': current.get('model'), 'restart_desktop': True}
    if state_file.exists():
        raise RuntimeError('A desktop restore record already exists. Restore before enabling again.')
    if current.get('model_provider', 'openai') != 'openai' or current.get('openai_base_url'):
        raise RuntimeError('A custom provider/endpoint already exists; refusing to replace it')
    build_catalog()
    values = activation_values()
    original = {key: json.dumps(current[key]) if key in current else None for key in values}
    updated = replace_roots(text, values)
    tomllib.loads(updated)
    backup = STATE / ('config-before-enable-' + str(time.time_ns()) + '.toml')
    backup.write_text(text, encoding='utf-8')
    state_file.write_text(json.dumps({'original': original, 'active': values, 'backup': str(backup)}, indent=2))
    CONFIG.write_text(updated, encoding='utf-8')
    return {'enabled': True, 'added_model': MODEL, 'default_model': current.get('model'), 'default_provider': current.get('model_provider', 'openai'), 'restart_desktop': True, 'backup_saved': True}


def restore():
    state_file = STATE / 'desktop-restore.json'
    if not state_file.exists():
        return {'restored': False, 'reason': 'Desktop provider was never enabled'}
    state = json.loads(state_file.read_text())
    text = CONFIG.read_text(encoding='utf-8')
    current = tomllib.loads(text)
    for key, value in state['active'].items():
        if current.get(key) != tomllib.loads('value = ' + value)['value']:
            raise RuntimeError(f'{key} was changed after activation. Refusing to overwrite it.')
    updated = replace_roots(text, state['original'])
    tomllib.loads(updated)
    CONFIG.write_text(updated, encoding='utf-8')
    state_file.unlink()
    return {'restored': True, 'restart_desktop': True}


def start_quietly():
    try:
        start()
    except Exception:
        pass


TOOLS = {
    'status': ('Check the local Tandem bridge and router. No model request or account charge.', status),
    'enable': ('Add Claude Opus 5.5 (Claude Code) to the Codex model picker by routing model requests through the local Tandem router. GPT stays the default. The user must restart Codex afterwards.', enable),
    'restore': ('Remove Tandem from the model picker and restore the direct OpenAI route. The user must restart Codex afterwards.', restore),
}


def mcp():
    # Management only: inference goes through the model provider, never a tool.
    for line in sys.stdin:
        row = json.loads(line)
        if 'id' not in row:
            continue
        try:
            method = row.get('method')
            if method == 'initialize':
                # Answer within Codex's MCP startup timeout; `status` reports any startup failure.
                threading.Thread(target=start_quietly, daemon=True).start()
                result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'tandem', 'version': '0.1.0'}}
            elif method == 'tools/list':
                result = {'tools': [{'name': name, 'description': description, 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}} for name, (description, _) in TOOLS.items()]}
            elif method == 'tools/call' and row.get('params', {}).get('name') in TOOLS:
                result = {'content': [{'type': 'text', 'text': json.dumps(TOOLS[row['params']['name']][1]())}]}
            elif method == 'ping':
                result = {}
            else:
                raise ValueError('Unknown method')
            print(json.dumps({'jsonrpc': '2.0', 'id': row['id'], 'result': result}), flush=True)
        except Exception as error:
            print(json.dumps({'jsonrpc': '2.0', 'id': row['id'], 'error': {'code': -32603, 'message': str(error)}}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'status', 'enable-desktop', 'restore-desktop', 'mcp'])
    args = parser.parse_args()
    actions = {'start': start, 'stop': stop, 'status': status, 'enable-desktop': enable, 'restore-desktop': restore, 'mcp': mcp}
    result = actions[args.action]()
    if result is not None:
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
