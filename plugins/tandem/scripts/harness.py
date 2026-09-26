"""Claude Code as the agent harness behind a Codex model entry.

Claude Code runs its own loop, tools, subagents, MCP servers and context management. Codex's
shell and patch tools are relayed to Claude Code over MCP; each relayed call is returned to
Codex as a native function call, so Codex executes it and renders its own command/diff cards,
then the output is handed back to the waiting Claude Code process.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from native.cli import INSTALL_HINT, MODEL, own_process_group, windows_environment, kill_process_tree, normalize_input_schema, resolve_claude
from responses import ProtocolError

HARNESS_MODEL = 'claude-opus-5-5-claude-code'
RELAY_SERVER = 'codex'
RELAY_PREFIX = f'mcp__{RELAY_SERVER}__'
SHELL_TOOLS = ('exec_command', 'write_stdin', 'shell', 'shell_command')
RELAY_TOOLS = SHELL_TOOLS + ('apply_patch', 'update_plan')
# Built-ins replaced by relayed Codex tools, plus ones that cannot work in a headless session.
SHELL_BUILTINS = ('Bash', 'PowerShell', 'BashOutput', 'KillShell', 'KillBash', 'Monitor', 'REPL')
EDIT_BUILTINS = ('Edit', 'Write', 'MultiEdit', 'NotebookEdit')
HEADLESS_BUILTINS = ('AskUserQuestion', 'EnterPlanMode', 'ExitPlanMode', 'EnterWorktree', 'ExitWorktree', 'CronCreate', 'CronDelete', 'CronList', 'RemoteTrigger', 'ScheduleWakeup', 'PushNotification')
MODEL_ITEMS = ('reasoning', 'function_call', 'custom_tool_call', 'compaction', 'local_shell_call', 'web_search_call', 'image_generation_call')
CONTEXT_PREFIXES = ('# AGENTS.md instructions', '<environment_context>', '<user_instructions>')
EFFORTS = {'minimal': 'low', 'low': 'low', 'medium': 'medium', 'high': 'high', 'xhigh': 'xhigh', 'max': 'max'}
# Claude Code internals with nothing useful to show.
QUIET_TOOLS = ('ToolSearch',)
# Shown with Codex's native web search rows. The id prefix lets the router drop them before GPT requests.
WEB_SEARCH_PREFIX = 'ws_tandem_'
KEEPALIVE_SECONDS = 15
IDLE_SECONDS = 3600
TRANSCRIPT_LIMIT = 200_000


def thread_id(body):
    meta = body.get('client_metadata') or {}
    return meta.get('thread_id') or meta.get('session_id') or body.get('prompt_cache_key')


def input_rows(body):
    rows = body.get('input', [])
    return [{'role': 'user', 'content': rows}] if isinstance(rows, str) else rows


def split_input(rows):
    """History already seen by Claude Code, and the new host-side rows since its last output."""
    last = -1
    for index, row in enumerate(rows):
        kind = row.get('type', 'message')
        if kind in MODEL_ITEMS or (kind == 'message' and row.get('role') == 'assistant'):
            last = index
    return rows[:last + 1], rows[last + 1:]


def texts(content):
    if isinstance(content, str):
        return content
    return ''.join(part.get('text', '') for part in content or [] if isinstance(part, dict))


def find_cwd(rows):
    for row in reversed(rows):
        if row.get('type', 'message') == 'message':
            match = re.search(r'<cwd>(.*?)</cwd>', texts(row.get('content')), re.S)
            if match and Path(match.group(1).strip()).is_dir():
                return match.group(1).strip()
    return str(Path.home())


def claude_blocks(content):
    if isinstance(content, str):
        return [{'type': 'text', 'text': content}] if content else []
    blocks = []
    for part in content or []:
        kind = part.get('type')
        if kind in ('input_text', 'output_text', 'text'):
            blocks.append({'type': 'text', 'text': part.get('text', '')})
        elif kind in ('input_image', 'image_url'):
            url = part.get('image_url')
            url = url if isinstance(url, str) else (url or {}).get('url', '')
            if url.startswith('data:') and ';base64,' in url:
                media, data = url[5:].split(';base64,', 1)
                blocks.append({'type': 'image', 'source': {'type': 'base64', 'media_type': media, 'data': data}})
            else:
                blocks.append({'type': 'text', 'text': f'[image: {url}]'})
        elif kind == 'input_file' and part.get('file_data', '').startswith('data:application/pdf;base64,'):
            blocks.append({'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': part['file_data'].split(',', 1)[1]}})
    return blocks


def mcp_result(output):
    """A Codex tool output as an MCP tools/call result."""
    if isinstance(output, str):
        return {'content': [{'type': 'text', 'text': output}], 'isError': False}
    content = []
    for part in output or []:
        kind = part.get('type')
        if kind in ('input_text', 'output_text', 'text'):
            content.append({'type': 'text', 'text': part.get('text', '')})
        elif kind == 'input_image' and str(part.get('image_url', '')).startswith('data:'):
            media, data = part['image_url'][5:].split(';base64,', 1)
            content.append({'type': 'image', 'data': data, 'mimeType': media})
    return {'content': content or [{'type': 'text', 'text': ''}], 'isError': False}


def mcp_error(message):
    return {'content': [{'type': 'text', 'text': message}], 'isError': True}


def relay_manifest(tools):
    manifest, kinds = [], {}

    def add(tool, namespace=None):
        if tool.get('type') == 'namespace':
            if namespace is not None:
                raise ProtocolError('Nested tool namespaces are unsupported')
            for child in tool.get('tools', []):
                add(child, tool['name'])
            return
        name = tool.get('name')
        if tool.get('type') not in ('function', 'custom') or name not in RELAY_TOOLS:
            return
        if name in kinds:
            raise ProtocolError(f'Ambiguous Codex relay tool: {name}')
        description = tool.get('description', '')
        if tool['type'] == 'custom':
            schema = {'type': 'object', 'properties': {'input': {'type': 'string', 'description': 'The complete raw tool input.'}}, 'required': ['input'], 'additionalProperties': False}
            grammar = (tool.get('format') or {}).get('definition')
            description += '\n\nPass the complete raw input in the `input` string.' + (f'\nInput grammar (lark):\n{grammar}' if grammar else '')
        else:
            schema = normalize_input_schema(tool.get('parameters') or {'type': 'object', 'properties': {}})
        manifest.append({'name': name, 'description': description, 'inputSchema': schema})
        kinds[name] = {'type': tool['type'], 'namespace': namespace}

    for tool in tools or []:
        add(tool)
    return manifest, kinds


def system_note(kinds):
    lines = ['You are running inside the Codex desktop app, which shows your work to the user. Codex executes the relayed tools below and shows each call as a native Codex card:']
    if any(name in kinds for name in SHELL_TOOLS):
        shell = next(name for name in SHELL_TOOLS if name in kinds and name != 'write_stdin')
        lines.append(f'- Run shell commands with {RELAY_PREFIX}{shell}' + (f' (continue interactive sessions with {RELAY_PREFIX}write_stdin)' if 'write_stdin' in kinds else '') + '. The Bash and PowerShell tools are unavailable.')
    if 'apply_patch' in kinds:
        lines.append(f'- Create, edit, move and delete files only with {RELAY_PREFIX}apply_patch. The Edit and Write tools are unavailable.')
    if 'update_plan' in kinds:
        lines.append(f'- Track multi-step work with {RELAY_PREFIX}update_plan.')
    lines.append('Read, Grep, Glob, web tools, skills, subagents and your other MCP servers work as usual.')
    return '\n'.join(lines)


def web_search_action(name, args):
    if name == 'WebSearch' and args.get('query'):
        return {'type': 'search', 'query': str(args['query'])}
    if name == 'WebFetch' and args.get('url'):
        return {'type': 'open_page', 'url': str(args['url'])}
    return None


def activity_label(name, args):
    def code(value):
        return '`' + str(value).replace('`', "'")[:200] + '`'
    if name == 'Read':
        return f"**Read** {code(args.get('file_path', ''))}"
    if name == 'Grep':
        return f"**Searched** for {code(args.get('pattern', ''))}" + (f" in {code(args['path'])}" if args.get('path') else '')
    if name == 'Glob':
        return f"**Listed files** matching {code(args.get('pattern', ''))}"
    if name == 'WebSearch':
        return f"**Searched the web** for {code(args.get('query', ''))}"
    if name == 'WebFetch':
        return f"**Fetched** {code(args.get('url', ''))}"
    if name in ('Agent', 'Task'):
        return f"**Started subagent**: {args.get('description') or args.get('subagent_type') or 'task'}"
    if name == 'Skill':
        return f"**Using skill** {code(args.get('skill') or args.get('command', ''))}"
    if name == 'TodoWrite':
        return '**Updated task list**'
    if name.startswith('mcp__'):
        server, _, tool = name[5:].partition('__')
        return f'**{server}**: {tool}'
    return f'**{name}**'


def transcript(prefix, codec):
    lines = []
    for row in prefix:
        kind = row.get('type', 'message')
        if kind == 'message' and row.get('role') in ('user', 'assistant'):
            text = texts(row.get('content')).strip()
            if text and not text.startswith(CONTEXT_PREFIXES):
                lines.append(f"{row['role'].capitalize()}: {text}")
        elif kind in ('function_call', 'custom_tool_call'):
            lines.append(f"Tool call {row.get('name')}: {(row.get('arguments') or row.get('input') or '')[:4000]}")
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            output = row.get('output')
            lines.append('Tool result: ' + (output if isinstance(output, str) else texts(output))[:4000])
        elif kind == 'compaction':
            carrier = codec.decode(row.get('encrypted_content'))
            if not carrier or carrier.get('type') != 'codex_compaction':
                raise ProtocolError('Claude Code cannot decode GPT compaction history. Start a new Claude Code chat.')
            lines.append('Earlier conversation summary: ' + carrier['summary'])
    if not lines:
        return None
    body = '\n\n'.join(lines)[-TRANSCRIPT_LIMIT:]
    return 'Synchronize with this conversation history from Codex. It is context, not a request to repeat completed work:\n\n' + body


class Call:
    def __init__(self, call_id, name, spec, arguments):
        self.id, self.name, self.kind, self.arguments = call_id, name, spec['type'], arguments
        self.namespace = spec['namespace']
        self.emitted = self.arrived = False
        self.result = None

    def item(self):
        item = {'id': 'fc_' + uuid.uuid4().hex, 'type': 'function_call' if self.kind == 'function' else 'custom_tool_call', 'call_id': self.id, 'name': self.name, 'status': 'completed'}
        if self.namespace:
            item['namespace'] = self.namespace
        if self.kind == 'custom':
            item['input'] = str(self.arguments.get('input', ''))
        else:
            item['arguments'] = json.dumps(self.arguments, ensure_ascii=False)
        return item


class Output:
    """Responses stream for one Codex request."""

    def __init__(self, codec, thread, emit):
        self.codec, self.write = codec, emit
        self.id = 'resp_' + uuid.uuid4().hex
        self.sequence = 0
        self.output = []
        # Marks these items as local so the router never replays them to OpenAI.
        self.carrier = codec.encode({'type': 'claude_code', 'thread': thread})
        self.message = self.reasoning = None
        self.searches = {}
        self.last_emit = time.monotonic()
        self.usage = {}
        self.context = self.cached = 0
        self.checkpoint = None

    def send(self, kind, **fields):
        self.sequence += 1
        self.write({'type': kind, 'sequence_number': self.sequence, **fields})
        self.last_emit = time.monotonic()

    def response(self, status, usage=None, error=None):
        return {'id': self.id, 'object': 'response', 'created_at': int(time.time()), 'status': status, 'model': HARNESS_MODEL, 'output': self.output, 'usage': usage, 'error': error, 'incomplete_details': None}

    def begin(self):
        self.send('response.created', response=self.response('in_progress'))
        self.send('response.in_progress', response=self.response('in_progress'))

    def keepalive(self):
        if time.monotonic() - self.last_emit >= KEEPALIVE_SECONDS:
            self.send('response.in_progress', response=self.response('in_progress'))

    def add(self, item):
        self.output.append(item)
        index = len(self.output) - 1
        self.send('response.output_item.added', output_index=index, item=dict(item))
        return index

    def done(self, index):
        self.send('response.output_item.done', output_index=index, item=self.output[index])

    def text(self, delta):
        self.close_reasoning()
        if self.message is None:
            item = {'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant', 'status': 'in_progress', 'content': [], 'phase': 'commentary'}
            index = self.add(item)
            self.send('response.content_part.added', item_id=item['id'], output_index=index, content_index=0, part={'type': 'output_text', 'text': '', 'annotations': []})
            self.message = [item, index, '']
        self.message[2] += delta
        self.send('response.output_text.delta', item_id=self.message[0]['id'], output_index=self.message[1], content_index=0, delta=delta)

    def close_message(self, phase):
        if self.message is None:
            return
        (item, index, text), self.message = self.message, None
        part = {'type': 'output_text', 'text': text, 'annotations': []}
        item.update(status='completed', phase=phase, content=[part])
        self.send('response.output_text.done', item_id=item['id'], output_index=index, content_index=0, text=text)
        self.send('response.content_part.done', item_id=item['id'], output_index=index, content_index=0, part=part)
        self.done(index)

    def open_reasoning(self):
        self.close_reasoning()
        item = {'id': 'rs_' + uuid.uuid4().hex, 'type': 'reasoning', 'summary': [], 'encrypted_content': self.carrier}
        index = self.add(item)
        self.send('response.reasoning_summary_part.added', item_id=item['id'], output_index=index, summary_index=0, part={'type': 'summary_text', 'text': ''})
        self.reasoning = [item, index, '']

    def thinking(self, delta):
        if self.reasoning is None:
            self.open_reasoning()
        self.reasoning[2] += delta
        self.send('response.reasoning_summary_text.delta', item_id=self.reasoning[0]['id'], output_index=self.reasoning[1], summary_index=0, delta=delta)

    def close_reasoning(self):
        if self.reasoning is None:
            return
        (item, index, text), self.reasoning = self.reasoning, None
        part = {'type': 'summary_text', 'text': text}
        self.send('response.reasoning_summary_text.done', item_id=item['id'], output_index=index, summary_index=0, text=text)
        self.send('response.reasoning_summary_part.done', item_id=item['id'], output_index=index, summary_index=0, part=part)
        item['summary'] = [part] if text else []
        self.done(index)

    def activity(self, text):
        self.close_message('commentary')
        self.open_reasoning()
        self.thinking(text)
        self.close_reasoning()

    def web_search(self, tool_use_id, action):
        self.close_reasoning()
        self.close_message('commentary')
        item = {'id': WEB_SEARCH_PREFIX + uuid.uuid4().hex, 'type': 'web_search_call', 'status': 'in_progress', 'action': action}
        self.searches[tool_use_id] = self.add(item)

    def web_search_done(self, tool_use_id):
        index = self.searches.pop(tool_use_id, None)
        if index is not None:
            self.output[index]['status'] = 'completed'
            self.done(index)

    def call(self, call):
        self.close_reasoning()
        self.close_message('commentary')
        self.done(self.add(call.item()))

    def track_usage(self, message):
        usage = message.get('usage') or {}
        if not isinstance(usage.get('input_tokens'), int):
            return
        self.usage[message.get('id')] = usage.get('output_tokens', 0)
        self.cached = usage.get('cache_read_input_tokens', 0)
        self.context = usage['input_tokens'] + self.cached + usage.get('cache_creation_input_tokens', 0)

    def finish(self, final):
        for tool_use_id in list(self.searches):
            self.web_search_done(tool_use_id)
        self.close_reasoning()
        self.close_message('final_answer' if final else 'commentary')
        # Every response ends in an opaque checkpoint, even if it had no thinking.
        marker = {'id': 'rs_' + uuid.uuid4().hex, 'type': 'reasoning', 'summary': [], 'encrypted_content': self.carrier}
        self.done(self.add(marker))
        self.checkpoint = self.carrier
        output = sum(self.usage.values())
        usage = {'input_tokens': self.context, 'output_tokens': output, 'total_tokens': self.context + output, 'input_tokens_details': {'cached_tokens': self.cached}, 'output_tokens_details': {'reasoning_tokens': 0}}
        self.send('response.completed', response=self.response('completed', usage))

    def fail(self, message):
        for tool_use_id in list(self.searches):
            self.web_search_done(tool_use_id)
        self.close_reasoning()
        self.close_message('commentary')
        self.send('response.failed', response=self.response('failed', error={'code': 'claude_code_error', 'message': message}))


class Session:
    """One long-lived Claude Code process for one Codex thread."""

    def __init__(self, harness, thread, session_id, cwd, effort, manifest, kinds, resume):
        self.thread, self.session_id, self.cwd, self.effort, self.resumed = thread, session_id, cwd, effort, resume
        self.kinds = kinds
        self.token = secrets.token_urlsafe(32)
        self.events = queue.Queue()
        self.cond = threading.Condition()
        self.busy = threading.Lock()
        self.stdin_lock = threading.Lock()
        self.calls = {}
        self.sent_context = set()
        self.checkpoint = None
        self.alive = True
        self.seen_output = False
        self.turn_active = False
        self.last_active = time.monotonic()
        self.stderr_tail = ''
        if not any(name in kinds for name in ('exec_command', 'shell', 'shell_command')) or 'apply_patch' not in kinds:
            raise ProtocolError('Claude Code requires Codex shell and apply_patch relays; native shell and editing tools stay disabled.')
        env = windows_environment(dict(os.environ))
        env['MCP_TOOL_TIMEOUT'] = str(24 * 3600 * 1000)
        # Codex shows one response per turn. Background subagents or shells would let Claude Code end the
        # turn early and continue later where Codex can't see it, so keep all work in the foreground.
        env['CLAUDE_CODE_DISABLE_BACKGROUND_TASKS'] = '1'
        resolved = resolve_claude(harness.command, env)
        if resolved is None:
            raise RuntimeError(INSTALL_HINT)
        self.tmp = Path(tempfile.mkdtemp(prefix='claude-code-harness-'))
        command = resolved + ['-p', '--model', MODEL + '[1m]', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--include-partial-messages', '--forward-subagent-text', '--permission-mode', 'bypassPermissions', '--effort', effort, '--thinking-display', 'summarized']
        command += ['--resume', session_id] if resume else ['--session-id', session_id]
        disallowed = [*HEADLESS_BUILTINS, *SHELL_BUILTINS, *EDIT_BUILTINS]
        if 'update_plan' in kinds:
            disallowed.append('TodoWrite')
        command += ['--disallowedTools', ','.join(disallowed)]
        if manifest:
            (self.tmp / 'tools.json').write_text(json.dumps(manifest), encoding='utf-8')
            (self.tmp / 'token').write_text(self.token, encoding='utf-8')
            relay = {'mcpServers': {RELAY_SERVER: {'command': sys.executable, 'args': [str(Path(__file__).with_name('native') / 'relay_mcp.py'), str(self.tmp / 'tools.json'), str(harness.port), str(self.tmp / 'token')]}}}
            command += ['--append-system-prompt', system_note(kinds), '--mcp-config', json.dumps(relay)]
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', cwd=cwd, env=env, **own_process_group())
        except Exception:
            shutil.rmtree(self.tmp, ignore_errors=True)
            raise
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_errors, daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('type') == 'assistant':
                    self._register(event)
                self.events.put(event)
        finally:
            with self.cond:
                self.alive = False
                self.cond.notify_all()
            self.events.put(None)

    def _read_errors(self):
        for line in self.process.stderr:
            self.stderr_tail = (self.stderr_tail + line)[-4000:]

    def _register(self, event):
        with self.cond:
            for block in (event.get('message') or {}).get('content') or []:
                name = block.get('name', '')
                if block.get('type') == 'tool_use' and name.startswith(RELAY_PREFIX) and block.get('id') not in self.calls:
                    name = name[len(RELAY_PREFIX):]
                    if name in self.kinds:
                        self.calls[block['id']] = Call(block['id'], name, self.kinds[name], block.get('input') or {})
            self.cond.notify_all()

    def relay(self, name, arguments, tool_use_id=None):
        """Called by the relay MCP server; blocks until Codex returns the call's output."""
        canonical = json.dumps(arguments, sort_keys=True)
        deadline = time.monotonic() + 30
        with self.cond:
            self.last_active = time.monotonic()
            while True:
                call = self.calls.get(tool_use_id) if tool_use_id else next((c for c in self.calls.values() if not c.arrived and c.name == name and json.dumps(c.arguments, sort_keys=True) == canonical), None)
                if call is not None:
                    break
                if not self.alive or time.monotonic() > deadline:
                    return mcp_error('Codex relay could not match this tool call.')
                self.cond.wait(.2)
            call.arrived = True
            self.events.put({'type': 'relay_waiting'})
            while call.result is None:
                if not self.alive:
                    return mcp_error('The Claude Code session ended before Codex returned this result.')
                self.cond.wait(1)
            self.last_active = time.monotonic()
            return call.result

    def deliver(self, results):
        """Hand Codex tool outputs to waiting calls; returns outputs no live call is waiting for."""
        leftovers = {}
        with self.cond:
            for call_id, output in results.items():
                call = self.calls.get(call_id)
                if call is None:
                    leftovers[call_id] = output
                elif call.result is None:
                    call.result = mcp_result(output)
            for call in self.calls.values():
                if call.emitted and call.result is None:
                    call.result = mcp_error('Codex did not run this call (the turn was interrupted).')
            self.cond.notify_all()
        return leftovers

    def user_blocks(self, rows):
        blocks = []
        for row in rows:
            if row.get('type', 'message') != 'message' or row.get('role') != 'user':
                continue
            content = claude_blocks(row.get('content'))
            text = texts(row.get('content')).strip()
            if text.startswith(CONTEXT_PREFIXES):
                if text in self.sent_context:
                    continue
                self.sent_context.add(text)
            blocks.extend(content)
        return blocks

    def send(self, blocks):
        frame = {'type': 'user', 'message': {'role': 'user', 'content': blocks}}
        with self.stdin_lock:
            self.process.stdin.write(json.dumps(frame, ensure_ascii=False) + '\n')
            self.process.stdin.flush()
        self.turn_active = True

    def should_yield(self):
        with self.cond:
            waiting = [c for c in self.calls.values() if c.arrived and c.result is None]
            return bool(waiting) and all(c.emitted for c in waiting)

    def mark_emitted(self, call):
        with self.cond:
            call.emitted = True

    def kill(self):
        with self.cond:
            self.alive = False
            self.cond.notify_all()
        kill_process_tree(self.process)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)


class Harness:
    def __init__(self, codec, state, port, command=None):
        self.codec, self.port = codec, port
        self.command = command or [os.environ.get('TANDEM_CLAUDE') or 'claude']
        self.store = Path(state) / 'harness-sessions.json'
        self.sessions, self.tokens = {}, {}
        self.lock = threading.RLock()
        self.thread_locks = {}
        self.closed = threading.Event()
        threading.Thread(target=self._reap, daemon=True).start()

    def thread_lock(self, thread):
        with self.lock:
            return self.thread_locks.setdefault(thread, threading.RLock())

    def _mapping(self):
        with self.lock:
            try:
                return json.loads(self.store.read_text(encoding='utf-8'))
            except FileNotFoundError:
                return {}

    def _remember(self, thread, session_id, cwd, checkpoint=None):
        with self.lock:
            mapping = self._mapping()
            if session_id is None:
                mapping.pop(thread, None)
            else:
                mapping[thread] = {'session_id': session_id, 'cwd': cwd, 'checkpoint': checkpoint}
            temporary = self.store.with_name(self.store.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_text(json.dumps(mapping, indent=2), encoding='utf-8')
                os.replace(temporary, self.store)
            finally:
                temporary.unlink(missing_ok=True)

    def _reap(self):
        while not self.closed.wait(60):
            with self.lock:
                sessions = list(self.sessions.values())
            for session in sessions:
                guard = self.thread_lock(session.thread)
                if guard.acquire(blocking=False):
                    try:
                        if time.monotonic() - session.last_active > IDLE_SECONDS and not session.busy.locked():
                            self.discard(session)
                    finally:
                        guard.release()

    def discard(self, session):
        with self.lock:
            if self.sessions.get(session.thread) is session:
                del self.sessions[session.thread]
            self.tokens.pop(session.token, None)
        session.kill()

    def close(self):
        self.closed.set()
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            self.discard(session)

    def by_token(self, token):
        with self.lock:
            return self.tokens.get(token)

    def acquire(self, thread, cwd, effort, manifest, kinds, fresh_only=False):
        with self.thread_lock(thread):
            return self._acquire(thread, cwd, effort, manifest, kinds, fresh_only)

    def _acquire(self, thread, cwd, effort, manifest, kinds, fresh_only):
        with self.lock:
            if self.closed.is_set():
                raise ProtocolError('The Claude Code bridge is shutting down')
            session = self.sessions.get(thread)
            if session is not None and session.alive:
                with session.cond:
                    waiting = any(c.result is None for c in session.calls.values())
                if session.kinds == kinds and (session.effort == effort or waiting):
                    return session, False
                if waiting:
                    raise ProtocolError('Codex changed its relay tools while Claude Code was waiting for a tool result')
            stale = session
        if stale is not None:
            self.discard(stale)
        mapping = None if fresh_only else self._mapping().get(thread)
        if mapping and Path(mapping['cwd']).is_dir():
            session = Session(self, thread, mapping['session_id'], mapping['cwd'], effort, manifest, kinds, resume=True)
            session.checkpoint = mapping.get('checkpoint')
        else:
            session = Session(self, thread, str(uuid.uuid4()), cwd, effort, manifest, kinds, resume=False)
            try:
                self._remember(thread, session.session_id, cwd)
            except Exception:
                session.kill()
                raise
        with self.lock:
            if self.closed.is_set():
                session.kill()
                raise ProtocolError('The Claude Code bridge is shutting down')
            self.sessions[thread] = session
            self.tokens[session.token] = session
        return session, not session.resumed

    def handle(self, body, io):
        thread = thread_id(body)
        if not thread:
            raise ProtocolError('Codex thread metadata is missing')
        if not body.get('stream'):
            raise ProtocolError('The Claude Code harness requires streaming')
        with self.thread_lock(thread):
            io.check()
            rows = input_rows(body)
            out = Output(self.codec, thread, io.emit)
            out.begin()
            try:
                if any(row.get('type') == 'compaction_trigger' for row in rows):
                    return self._compaction(thread, out)
                effort = EFFORTS.get((body.get('reasoning') or {}).get('effort') or 'medium', 'medium')
                manifest, kinds = relay_manifest(body.get('tools'))
                session, fresh = self.acquire(thread, find_cwd(rows), effort, manifest, kinds)
                if self._turn(session, fresh, rows, io, out) == 'resume_failed':
                    self.discard(session)
                    self._remember(thread, None, None)
                    session, fresh = self.acquire(thread, find_cwd(rows), effort, manifest, kinds, fresh_only=True)
                    self._turn(session, True, rows, io, out)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                raise
            except Exception as error:
                out.fail(str(error))

    def _turn(self, session, fresh, rows, io, out):
        with session.busy:
            session.last_active = time.monotonic()
            unseen = rows
            if not fresh and session.checkpoint:
                for index in range(len(rows) - 1, -1, -1):
                    if rows[index].get('encrypted_content') == session.checkpoint:
                        unseen = rows[index + 1:]
                        break
            prefix, suffix = split_input(unseen)
            # Rules may predate the last assistant output or a model switch. Send them independently.
            context = [row for row in rows if row.get('role') == 'user' and texts(row.get('content')).strip().startswith(CONTEXT_PREFIXES)]
            blocks = session.user_blocks(context)
            history = transcript(prefix, self.codec)
            if history:
                blocks.append({'type': 'text', 'text': history})
            blocks.extend(session.user_blocks(suffix))
            results = {row['call_id']: row.get('output', '') for row in suffix if row.get('type') in ('function_call_output', 'custom_tool_call_output')}
            leftovers = session.deliver(results)
            if leftovers:
                note = '\n\n'.join(f'Output of Codex tool call {call_id}:\n' + (output if isinstance(output, str) else texts(output))[:20000] for call_id, output in leftovers.items())
                blocks.append({'type': 'text', 'text': 'These tool calls finished in Codex while your session was restarting.\n\n' + note})
            try:
                if blocks:
                    session.send(blocks)
                elif not session.turn_active:
                    out.finish(final=True)
                    self._save_checkpoint(session, out)
                    return 'done'
                result = self._stream(session, out, io)
                self._save_checkpoint(session, out)
                return result
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                # Codex interrupted the turn. The session is saved; the next turn resumes it.
                self.discard(session)
                raise

    def _save_checkpoint(self, session, out):
        if out.checkpoint:
            self._remember(session.thread, session.session_id, session.cwd, out.checkpoint)
            session.checkpoint = out.checkpoint

    def _stream(self, session, out, io):
        streamed = set()
        current = None
        while True:
            io.check()
            try:
                event = session.events.get(timeout=.2)
            except queue.Empty:
                io.check()
                out.keepalive()
                continue
            if event is None:
                if session.resumed and not session.seen_output:
                    return 'resume_failed'
                out.fail('Claude Code exited unexpectedly. ' + session.stderr_tail.strip()[-800:])
                self.discard(session)
                return 'failed'
            session.seen_output = True
            session.last_active = time.monotonic()
            kind = event.get('type')
            if kind == 'stream_event' and event.get('parent_tool_use_id') is None:
                native = event.get('event') or {}
                native_type = native.get('type')
                if native_type == 'message_start':
                    current = (native.get('message') or {}).get('id')
                elif native_type == 'content_block_start':
                    block_type = (native.get('content_block') or {}).get('type')
                    if block_type in ('thinking', 'redacted_thinking'):
                        out.close_message('commentary')
                        out.open_reasoning()
                    elif block_type == 'tool_use':
                        out.close_reasoning()
                        out.close_message('commentary')
                    elif block_type == 'text':
                        out.close_reasoning()
                elif native_type == 'content_block_delta':
                    delta = native.get('delta') or {}
                    if delta.get('type') == 'text_delta':
                        out.text(delta.get('text', ''))
                        streamed.add(current)
                    elif delta.get('type') == 'thinking_delta':
                        out.thinking(delta.get('thinking', ''))
                        streamed.add(current)
                elif native_type == 'content_block_stop':
                    out.close_reasoning()
            elif kind == 'assistant':
                message = event.get('message') or {}
                main = event.get('parent_tool_use_id') is None
                if main:
                    out.track_usage(message)
                for block in message.get('content') or []:
                    block_type = block.get('type')
                    if main and message.get('id') not in streamed:
                        # Partial messages were not streamed for this message; show it whole.
                        if block_type == 'text' and block.get('text'):
                            out.text(block['text'])
                        elif block_type == 'thinking' and block.get('thinking'):
                            out.activity(block['thinking'])
                    if block_type != 'tool_use':
                        continue
                    name = block.get('name', '')
                    if name.startswith(RELAY_PREFIX):
                        call = session.calls.get(block.get('id'))
                        if call is not None and not call.emitted:
                            out.call(call)
                            session.mark_emitted(call)
                    elif main and web_search_action(name, block.get('input') or {}):
                        out.web_search(block.get('id'), web_search_action(name, block.get('input') or {}))
                    elif main and name not in QUIET_TOOLS:
                        out.activity(activity_label(name, block.get('input') or {}))
            elif kind == 'user' and event.get('parent_tool_use_id') is None:
                for block in (event.get('message') or {}).get('content') or []:
                    if isinstance(block, dict) and block.get('type') == 'tool_result':
                        out.web_search_done(block.get('tool_use_id'))
            elif kind == 'system' and event.get('subtype') == 'compact_boundary':
                out.activity('**Compacted context**')
            elif kind == 'result':
                session.turn_active = False
                if event.get('is_error'):
                    out.fail(str(event.get('result') or event.get('subtype') or 'Claude Code turn failed'))
                    return 'failed'
                out.finish(final=True)
                return 'done'
            if session.should_yield():
                # Claude Code is blocked on relayed calls: hand them to Codex to execute and render.
                out.finish(final=False)
                return 'yielded'

    def _compaction(self, thread, out):
        # Claude Code manages its own context, so Codex compaction only resets Codex's view.
        mapping = self._mapping().get(thread)
        if not mapping:
            raise ProtocolError('No Claude Code session exists to preserve during compaction')
        session_id = mapping['session_id']
        summary = f'This chat continues in Claude Code session {session_id}, which keeps and compacts its own context.'
        item = {'id': 'cmp_' + uuid.uuid4().hex, 'type': 'compaction', 'encrypted_content': self.codec.encode({'type': 'codex_compaction', 'summary': summary})}
        out.done(out.add(item))
        out.finish(final=False)
        # Codex may retain only the compaction item, so use it instead of the trailing reasoning marker.
        checkpoint = item['encrypted_content']
        self._remember(thread, session_id, mapping['cwd'], checkpoint)
        with self.lock:
            session = self.sessions.get(thread)
            if session is not None:
                session.checkpoint = checkpoint
