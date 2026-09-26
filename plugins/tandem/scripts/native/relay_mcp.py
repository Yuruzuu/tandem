"""Relays Claude Code's calls to Codex tools through the local bridge. No tool runs here."""
import json
from pathlib import Path
import sys
import threading
import urllib.request


def main():
    manifest = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    port, token = sys.argv[2], Path(sys.argv[3]).read_text(encoding='utf-8').strip()
    write_lock = threading.Lock()

    def reply(row_id, result):
        with write_lock:
            print(json.dumps({'jsonrpc': '2.0', 'id': row_id, 'result': result}), flush=True)

    def call(row_id, params):
        body = {'name': params.get('name'), 'arguments': params.get('arguments') or {}, 'tool_use_id': (params.get('_meta') or {}).get('claudecode/toolUseId')}
        request = urllib.request.Request(f'http://127.0.0.1:{port}/relay', data=json.dumps(body).encode(), method='POST', headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        try:
            # Blocks until Codex has executed the call and returned its output.
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
        except Exception as error:
            result = {'isError': True, 'content': [{'type': 'text', 'text': f'Codex relay failed: {error}'}]}
        reply(row_id, result)

    for line in sys.stdin:
        row = json.loads(line)
        method = row.get('method')
        if 'id' not in row:
            continue
        if method == 'initialize':
            version = (row.get('params') or {}).get('protocolVersion') or '2024-11-05'
            reply(row['id'], {'protocolVersion': version, 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'codex-relay', 'version': '1'}})
        elif method == 'tools/list':
            reply(row['id'], {'tools': manifest})
        elif method == 'tools/call':
            # Parallel calls must not wait on each other.
            threading.Thread(target=call, args=(row['id'], row.get('params') or {}), daemon=True).start()
        else:
            reply(row['id'], {})


if __name__ == '__main__':
    main()
