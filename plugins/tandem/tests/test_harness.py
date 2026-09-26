import http.client
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cryptography.fernet import Fernet
import bridge
import harness
from responses import CarrierCodec

FAKE = [sys.executable, str(Path(__file__).with_name('fake_claude.py'))]
TOOLS = [
    {'type': 'function', 'name': 'exec_command', 'description': 'Run a command', 'parameters': {'type': 'object', 'properties': {'cmd': {'type': 'string'}}}},
    {'type': 'custom', 'name': 'apply_patch', 'description': 'Edit files', 'format': {'type': 'grammar', 'syntax': 'lark', 'definition': 'start: patch'}},
    {'type': 'function', 'name': 'view_image', 'description': 'not relayed', 'parameters': {'type': 'object', 'properties': {}}},
]


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = Path(self.directory.name)
        self.codec = CarrierCodec(Fernet.generate_key())
        self.server = bridge.ThreadingHTTPServer(('127.0.0.1', 0), bridge.Handler)
        self.server.secret, self.server.codec, self.server.lock = 'fixture', self.codec, threading.Lock()
        self.server.clients = set()
        self.server.metrics = {'harness_requests': 0, 'active_requests': 0, 'cancelled': 0}
        self.server.harness = harness.Harness(self.codec, self.state, self.server.server_port, command=FAKE)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.harness.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def turn(self, rows, tools=None):
        body = {'model': harness.HARNESS_MODEL, 'stream': True, 'input': rows, 'tools': TOOLS if tools is None else tools, 'reasoning': {'effort': 'high'}, 'client_metadata': {'thread_id': 'thread-1'}}
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=30)
        conn.request('POST', '/v1/responses', body=json.dumps(body), headers={'Authorization': 'Bearer fixture', 'Content-Type': 'application/json'})
        events = [json.loads(line[6:]) for line in conn.getresponse().read().decode().splitlines() if line.startswith('data: ')]
        conn.close()
        return events

    def test_relayed_calls_become_codex_calls_and_results_resume_claude_code(self):
        rows = [
            {'role': 'developer', 'content': 'Codex harness preamble'},
            {'role': 'user', 'content': [{'type': 'input_text', 'text': f'<environment_context>\n  <cwd>{self.state}</cwd>\n</environment_context>'}]},
            {'role': 'user', 'content': 'hi'},
        ]
        first = self.turn(rows)
        self.assertEqual(first[-1]['type'], 'response.completed')
        output = first[-1]['response']['output']
        self.assertEqual([item['type'] for item in output], ['reasoning', 'message', 'function_call', 'reasoning', 'reasoning'])
        self.assertEqual(output[0]['summary'][0]['text'], 'Planning')
        self.assertTrue(output[0]['encrypted_content'].startswith('codex_claude_v1.'))
        self.assertEqual((output[1]['content'][0]['text'], output[1]['phase']), ('Running it.', 'commentary'))
        self.assertEqual((output[2]['name'], output[2]['call_id'], json.loads(output[2]['arguments'])), ('exec_command', 'toolu_1', {'cmd': 'echo hi'}))
        self.assertEqual(output[3]['summary'][0]['text'], '**Read** `a.txt`')
        self.assertIn('response.reasoning_summary_text.delta', [event['type'] for event in first])
        self.assertEqual(first[-1]['response']['usage']['input_tokens'], 150)

        rows += output + [{'type': 'function_call_output', 'call_id': 'toolu_1', 'output': 'hi'}]
        second = self.turn(rows)
        final = second[-1]['response']['output']
        self.assertEqual(second[-1]['type'], 'response.completed')
        message = next(item for item in final if item['type'] == 'message')
        self.assertEqual(message['phase'], 'final_answer')
        # The preamble is Codex's own harness prompt; the environment context and request reach Claude Code.
        self.assertTrue(message['content'][0]['text'].startswith('Done: hi (turn 1'))
        self.assertIn('hi', message['content'][0]['text'])
        self.assertNotIn('preamble', message['content'][0]['text'])

        # The same live process continues the next user turn.
        rows += final + [{'role': 'user', 'content': 'again'}]
        third = self.turn(rows)
        self.assertEqual(third[-1]['response']['output'][2]['call_id'], 'toolu_2')
        self.assertEqual(json.loads((self.state / 'harness-sessions.json').read_text())['thread-1']['cwd'], str(self.state))

    def test_background_subagent_continuation_stays_in_the_same_codex_turn(self):
        rows = [
            {'role': 'user', 'content': [{'type': 'input_text', 'text': f'<environment_context>\n  <cwd>{self.state}</cwd>\n</environment_context>'}]},
            {'role': 'user', 'content': 'count them in the background'},
        ]
        events = self.turn(rows)
        self.assertEqual(events[-1]['type'], 'response.completed')
        messages = [item for item in events[-1]['response']['output'] if item['type'] == 'message']
        self.assertEqual([(m['content'][0]['text'], m['phase']) for m in messages],
                         [('Waiting on the subagent.', 'commentary'), ('There are 2 files.', 'final_answer')])
        notes = [item['summary'][0]['text'] for item in events[-1]['response']['output'] if item['type'] == 'reasoning' and item['summary']]
        self.assertIn('**Subagent completed**: Count files', notes)

    def test_claude_code_web_tools_become_native_web_search_items(self):
        rows = [
            {'role': 'user', 'content': [{'type': 'input_text', 'text': f'<environment_context>\n  <cwd>{self.state}</cwd>\n</environment_context>'}]},
            {'role': 'user', 'content': 'search the web'},
        ]
        events = self.turn(rows)
        searches = [item for item in events[-1]['response']['output'] if item['type'] == 'web_search_call']
        self.assertEqual([item['action'] for item in searches], [{'type': 'search', 'query': 'tandem codex'}, {'type': 'open_page', 'url': 'https://example.com'}])
        self.assertTrue(all(item['status'] == 'completed' and item['id'].startswith(harness.WEB_SEARCH_PREFIX) for item in searches))
        added = [event['item']['status'] for event in events if event['type'] == 'response.output_item.added' and event['item']['type'] == 'web_search_call']
        self.assertEqual(added, ['in_progress', 'in_progress'])
        self.assertEqual(events[-1]['response']['output'][-2]['content'][0]['text'], 'Found it.')

    def test_manifest_relays_only_codex_shell_and_patch_tools(self):
        manifest, kinds = harness.relay_manifest(TOOLS)
        self.assertEqual(kinds, {'exec_command': {'type': 'function', 'namespace': None}, 'apply_patch': {'type': 'custom', 'namespace': None}})
        self.assertEqual(manifest[1]['inputSchema']['required'], ['input'])
        self.assertIn('start: patch', manifest[1]['description'])

    def test_compaction_keeps_claude_code_context(self):
        self.server.harness._remember('thread-1', 'saved-session', str(self.state))
        events = self.turn([{'role': 'user', 'content': 'x'}, {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'y'}]}, {'type': 'compaction_trigger'}])
        item = events[-1]['response']['output'][0]
        self.assertEqual(item['type'], 'compaction')
        self.assertEqual(self.codec.decode(item['encrypted_content'])['type'], 'codex_compaction')

    def test_prior_gpt_history_seeds_a_new_session(self):
        prefix = [{'role': 'user', 'content': 'build a parser'}, {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Parser built.'}]}]
        text = harness.transcript(prefix, self.codec)
        self.assertIn('User: build a parser', text)
        self.assertIn('Assistant: Parser built.', text)
        self.assertIsNone(harness.transcript([{'role': 'user', 'content': '<environment_context>x</environment_context>'}], self.codec))

    def complete_turn(self, rows, tools=None):
        first = self.turn(rows, tools)
        self.assertEqual(first[-1]['type'], 'response.completed', first[-1])
        output = first[-1]['response']['output']
        call = next(item for item in output if item['type'] == 'function_call')
        rows += output + [{'type': 'function_call_output', 'call_id': call['call_id'], 'output': 'fixture result'}]
        second = self.turn(rows, tools)
        self.assertEqual(second[-1]['type'], 'response.completed', second[-1])
        final = second[-1]['response']['output']
        rows += final
        return next(item for item in final if item['type'] == 'message')['content'][0]['text']

    def context(self):
        return {'role': 'user', 'content': f'# AGENTS.md instructions\nRULE_SENTINEL\n<environment_context><cwd>{self.state}</cwd></environment_context>'}

    def test_namespaced_relays_preserve_namespace_and_disable_native_writes(self):
        tools = [{'type': 'namespace', 'name': 'functions', 'tools': TOOLS}]
        rows = [self.context(), {'role': 'user', 'content': 'hi'}]
        events = self.turn(rows, tools)
        output = events[-1]['response']['output']
        call = next(item for item in output if item['type'] == 'function_call')
        self.assertEqual(call['namespace'], 'functions')
        session = self.server.harness.sessions['thread-1']
        command = session.process.args
        disabled = command[command.index('--disallowedTools') + 1].split(',')
        self.assertTrue(set(harness.SHELL_BUILTINS + harness.EDIT_BUILTINS).issubset(disabled))
        patch_call = harness.Call('patch', 'apply_patch', session.kinds['apply_patch'], {'input': 'patch text'}).item()
        self.assertEqual((patch_call['type'], patch_call['namespace']), ('custom_tool_call', 'functions'))
        rows += output + [{'type': 'function_call_output', 'call_id': call['call_id'], 'output': 'namespaced result'}]
        finished = self.turn(rows, tools)
        self.assertEqual(finished[-1]['type'], 'response.completed')

    def test_missing_relays_fail_without_launching_claude(self):
        for tools in ([], TOOLS[:1], TOOLS[1:]):
            with self.subTest(tools=tools), patch.object(harness.subprocess, 'Popen') as spawn:
                events = self.turn([{'role': 'user', 'content': 'hi'}], tools)
                self.assertEqual(events[-1]['type'], 'response.failed')
                self.assertIn('requires Codex shell and apply_patch relays', events[-1]['response']['error']['message'])
                spawn.assert_not_called()
        self.assertFalse(self.server.harness.sessions)

    def test_ambiguous_namespaced_tools_are_rejected(self):
        with self.assertRaisesRegex(harness.ProtocolError, 'Ambiguous'):
            harness.relay_manifest([TOOLS[0], {'type': 'namespace', 'name': 'functions', 'tools': TOOLS}])

    def test_switching_into_claude_preserves_initial_user_context(self):
        rows = [self.context(), {'role': 'user', 'content': 'original task'}, {'role': 'assistant', 'content': 'GPT completed task'}, {'role': 'user', 'content': 'continue'}]
        text = self.complete_turn(rows)
        self.assertIn('RULE_SENTINEL', text)
        self.assertIn('<environment_context>', text)
        self.assertIn('GPT completed task', text)

    def test_returning_from_gpt_delivers_only_unseen_history(self):
        rows = [self.context(), {'role': 'user', 'content': 'OLD_TASK_SENTINEL'}]
        self.complete_turn(rows)
        rows += [{'role': 'user', 'content': 'GPT_INSTRUCTION_SENTINEL'}, {'role': 'assistant', 'content': 'GPT_RESULT_SENTINEL'}, {'role': 'user', 'content': 'back to Claude'}]
        text = self.complete_turn(rows)
        self.assertIn('GPT_INSTRUCTION_SENTINEL', text)
        self.assertIn('GPT_RESULT_SENTINEL', text)
        self.assertNotIn('OLD_TASK_SENTINEL', text)
        self.assertNotIn('RULE_SENTINEL', text)

    def test_saved_checkpoint_survives_bridge_restart(self):
        rows = [self.context(), {'role': 'user', 'content': 'OLD_TASK_SENTINEL'}]
        self.complete_turn(rows)
        owner = self.server.harness
        saved = owner._mapping()['thread-1']
        self.assertEqual(saved['checkpoint'], rows[-1]['encrypted_content'])
        owner.close()
        self.server.harness = harness.Harness(self.codec, self.state, self.server.server_port, command=FAKE)
        rows += [{'role': 'user', 'content': 'new request'}]
        text = self.complete_turn(rows)
        self.assertNotIn('OLD_TASK_SENTINEL', text)
        self.assertIn('RULE_SENTINEL', text)
        self.assertIn('--resume', self.server.harness.sessions['thread-1'].process.args)

    def test_compaction_checkpoint_supports_next_turn(self):
        rows = [self.context(), {'role': 'user', 'content': 'original task'}]
        self.complete_turn(rows)
        compacted = self.turn(rows + [{'type': 'compaction_trigger'}])[-1]['response']['output']
        compaction = next(item for item in compacted if item['type'] == 'compaction')
        self.assertEqual(self.server.harness._mapping()['thread-1']['checkpoint'], compaction['encrypted_content'])
        text = self.complete_turn([self.context(), compaction, {'role': 'user', 'content': 'after compaction'}])
        self.assertIn('after compaction', text)
        self.assertNotIn('This chat continues in Claude Code session', text)

    def test_gpt_compaction_is_rejected_and_old_opus_summary_is_preserved(self):
        with self.assertRaisesRegex(harness.ProtocolError, 'cannot decode GPT'):
            harness.transcript([{'type': 'compaction', 'encrypted_content': 'opaque-gpt'}], self.codec)
        encrypted = self.codec.encode({'type': 'codex_compaction', 'summary': 'kept summary'})
        self.assertIn('kept summary', harness.transcript([{'type': 'compaction', 'encrypted_content': encrypted}], self.codec))

    def test_concurrent_new_chats_preserve_every_mapping(self):
        owner = self.server.harness
        original = owner._mapping

        def delayed_read():
            mapping = original()
            time.sleep(.01)
            return mapping

        with patch.object(owner, '_mapping', side_effect=delayed_read), ThreadPoolExecutor(max_workers=8) as pool:
            jobs = [pool.submit(owner._remember, f'thread-{i}', f'session-{i}', str(self.state)) for i in range(12)]
            for job in jobs:
                job.result(timeout=10)
        self.assertEqual(len(original()), 12)
        self.assertEqual(list(self.state.glob('*.tmp')), [])

    def test_failed_mapping_replace_keeps_previous_file(self):
        owner = self.server.harness
        owner._remember('old', 'session-old', str(self.state))
        before = owner.store.read_text()
        with patch.object(harness.os, 'replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                owner._remember('new', 'session-new', str(self.state))
        self.assertEqual(owner.store.read_text(), before)
        self.assertEqual(list(self.state.glob('*.tmp')), [])

    def test_overlapping_requests_acquire_one_session(self):
        owner = self.server.harness
        manifest, kinds = harness.relay_manifest(TOOLS)
        created = []

        def create(*args, **kwargs):
            time.sleep(.05)
            session = MagicMock()
            session.thread, session.session_id, session.cwd, session.token = args[1], args[2], args[3], 'fixture-token'
            session.effort, session.kinds = args[4], args[6]
            session.resumed, session.alive, session.calls = False, True, {}
            session.cond = threading.Condition()
            created.append(session)
            return session

        with patch.object(harness, 'Session', side_effect=create), ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(owner.acquire, 'same-thread', str(self.state), 'high', manifest, kinds) for _ in range(2)]
            acquired = [job.result(timeout=10)[0] for job in jobs]
        self.assertIs(acquired[0], acquired[1])
        self.assertEqual(len(created), 1)
        self.assertEqual(len(owner.tokens), 1)

    def test_idle_keepalive_and_disconnect_kill_the_owned_session(self):
        body = {'model': harness.HARNESS_MODEL, 'stream': True, 'tools': TOOLS,
                'input': [self.context(), {'role': 'user', 'content': 'SILENT_FIXTURE'}],
                'client_metadata': {'thread_id': 'thread-1'}}
        with patch.object(harness, 'KEEPALIVE_SECONDS', .1):
            conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
            conn.request('POST', '/v1/responses', body=json.dumps(body),
                         headers={'Authorization': 'Bearer fixture', 'Content-Type': 'application/json'})
            response = conn.getresponse()
            kinds = []
            while kinds.count('response.in_progress') < 2:
                line = response.readline().decode()
                if line.startswith('event: '):
                    kinds.append(line[7:].strip())
            session = self.server.harness.sessions['thread-1']
            response.close()
            conn.close()
            deadline = time.monotonic() + 5
            while self.server.metrics['active_requests'] and time.monotonic() < deadline:
                time.sleep(.02)
        self.assertEqual(kinds[:3], ['response.created', 'response.in_progress', 'response.in_progress'])
        self.assertIsNotNone(session.process.poll())
        self.assertNotIn('thread-1', self.server.harness.sessions)
        self.assertEqual(self.server.metrics['cancelled'], 1)


if __name__ == '__main__':
    unittest.main()
