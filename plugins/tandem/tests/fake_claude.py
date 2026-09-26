"""Stands in for `claude -p` stream-json: thinks, calls the Codex relay, then answers."""
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

args = sys.argv[1:]
relay = json.loads(args[args.index('--mcp-config') + 1])['mcpServers']['codex']['args']
port, token = relay[2], Path(relay[3]).read_text().strip()
session = args[args.index('--session-id') + 1] if '--session-id' in args else args[args.index('--resume') + 1]
Path(relay[0]).parent  # relay_mcp.py path is passed through untouched


def out(event):
    print(json.dumps({**event, 'session_id': session}), flush=True)


def stream(message_id, blocks):
    out({'type': 'stream_event', 'parent_tool_use_id': None, 'event': {'type': 'message_start', 'message': {'id': message_id}}})
    for index, block in enumerate(blocks):
        out({'type': 'stream_event', 'parent_tool_use_id': None, 'event': {'type': 'content_block_start', 'index': index, 'content_block': {'type': block['type']}}})
        if block['type'] == 'thinking':
            out({'type': 'stream_event', 'parent_tool_use_id': None, 'event': {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'thinking_delta', 'thinking': block['thinking']}}})
        elif block['type'] == 'text':
            out({'type': 'stream_event', 'parent_tool_use_id': None, 'event': {'type': 'content_block_delta', 'index': index, 'delta': {'type': 'text_delta', 'text': block['text']}}})
        out({'type': 'stream_event', 'parent_tool_use_id': None, 'event': {'type': 'content_block_stop', 'index': index}})
        out({'type': 'assistant', 'parent_tool_use_id': None, 'message': {'id': message_id, 'content': [block], 'usage': {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 50}}})
    out({'type': 'stream_event', 'parent_tool_use_id': None, 'event': {'type': 'message_stop'}})


turn = 0
for line in sys.stdin:
    frame = json.loads(line)
    turn += 1
    words = ' '.join(b.get('text', '') for b in frame['message']['content'] if b['type'] == 'text')
    out({'type': 'system', 'subtype': 'init', 'tools': []})
    if 'SILENT_FIXTURE' in words:
        time.sleep(60)
        continue
    if 'search the web' in words:
        # Claude Code runs its own web tools; the harness only reports them.
        stream(f'm{turn}w', [{'type': 'tool_use', 'id': 'toolu_ws', 'name': 'WebSearch', 'input': {'query': 'tandem codex'}},
                              {'type': 'tool_use', 'id': 'toolu_wf', 'name': 'WebFetch', 'input': {'url': 'https://example.com', 'prompt': 'x'}}])
        out({'type': 'user', 'parent_tool_use_id': None, 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'toolu_ws', 'content': 'results'}, {'type': 'tool_result', 'tool_use_id': 'toolu_wf', 'content': 'page'}]}})
        stream(f'm{turn}x', [{'type': 'text', 'text': 'Found it.'}])
        out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok'})
        continue
    stream(f'm{turn}a', [{'type': 'thinking', 'thinking': 'Planning'}, {'type': 'text', 'text': 'Running it.'},
                          {'type': 'tool_use', 'id': f'toolu_{turn}', 'name': 'mcp__codex__exec_command', 'input': {'cmd': 'echo hi'}},
                          {'type': 'tool_use', 'id': f'toolu_read{turn}', 'name': 'Read', 'input': {'file_path': 'a.txt'}}])
    request = urllib.request.Request(f'http://127.0.0.1:{port}/relay', method='POST', headers={'Authorization': 'Bearer ' + token},
                                     data=json.dumps({'name': 'exec_command', 'arguments': {'cmd': 'echo hi'}, 'tool_use_id': f'toolu_{turn}'}).encode())
    result = json.load(urllib.request.urlopen(request))
    stream(f'm{turn}b', [{'type': 'text', 'text': f"Done: {result['content'][0]['text']} (turn {turn}, saw {words!r}) background={os.environ.get('CLAUDE_CODE_DISABLE_BACKGROUND_TASKS')}"}])
    out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok'})
