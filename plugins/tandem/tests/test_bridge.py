from contextlib import ExitStack, contextmanager
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cryptography.fernet import Fernet
import control
import bridge
from harness import HARNESS_MODEL
from responses import CarrierCodec, ProtocolError


@contextmanager
def codex_home(root, config):
    """A throwaway Codex home with a cached GPT catalog and Tandem state."""
    (root / 'models_cache.json').write_text(json.dumps({'models': [{'slug': 'gpt-6-sol'}, {'slug': HARNESS_MODEL, 'stale': True}]}))
    with ExitStack() as stack:
        for name, value in (('CONFIG', config), ('STATE', root / 'tandem'), ('CATALOG', root / 'tandem' / 'models.json'), ('CODEX_HOME', root)):
            stack.enter_context(patch.object(control, name, value))
        stack.enter_context(patch.object(control, 'start', return_value={}))
        stack.enter_context(patch.object(control, 'settings', return_value={'secret': 'test-only'}))
        yield


class ConfigurationTests(unittest.TestCase):
    def test_catalog_combines_cached_gpt_models_with_tandem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with codex_home(root, root / 'config.toml'):
                control.build_catalog()
                models = json.loads((root / 'tandem' / 'models.json').read_text())['models']
        self.assertEqual([model['slug'] for model in models], ['gpt-6-sol', HARNESS_MODEL])
        self.assertNotIn('stale', models[1])

    def test_enable_restore_preserves_unrelated_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.toml'
            original = 'model = "gpt-6-sol"\nmodel_context_window = 1000000\n\n[features]\nkeep = true\n'
            config.write_text(original)
            with codex_home(root, config):
                control.enable()
                self.assertEqual(control.tomllib.loads(config.read_text())['model'], 'gpt-6-sol')
                config.write_text(config.read_text().replace('keep = true', 'keep = false'))
                control.restore()
                self.assertEqual(config.read_text(), original.replace('keep = true', 'keep = false'))

    def test_restore_refuses_to_overwrite_changed_owned_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.toml'
            config.write_text('model = "original"\n')
            with codex_home(root, config):
                control.enable()
                config.write_text(config.read_text().replace('models.json', 'changed-models.json'))
                with self.assertRaises(RuntimeError):
                    control.restore()

    def test_valid_toml_formatting_roundtrips(self):
        variants = [
            'model = "gpt-6-sol"',
            'model = "gpt-6-sol"\n  model_catalog_json = "old.json"\n',
            'model = "gpt-6-sol"\n"model_catalog_json" = \'old.json\'\n',
            'model = "gpt-6-sol"\nmodel_catalog_json = """\nold.json"""\n',
            'model = "gpt-6-sol"\nnote = """\n[not_a_table]\nmodel_catalog_json = "not_a_setting"\n"""\n[features]\nkeep = true\n',
            'model = "gpt-6-sol"\nproviders = [\n"one",\n"two"\n]\n',
        ]
        for original in variants:
            with self.subTest(original=original), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / 'config.toml'
                config.write_text(original)
                expected = control.tomllib.loads(original)
                with codex_home(root, config):
                    control.enable()
                    enabled = control.tomllib.loads(config.read_text())
                    self.assertEqual(enabled['model'], expected['model'])
                    self.assertEqual(enabled['model_catalog_json'], str(root / 'tandem' / 'models.json'))
                    control.restore()
                self.assertEqual(control.tomllib.loads(config.read_text()), expected)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.server = bridge.ThreadingHTTPServer(('127.0.0.1', 0), bridge.Handler)
        self.server.secret = 'fixture'
        self.server.lock = threading.Lock()
        self.server.metrics = {'harness_requests': 0, 'active_requests': 0, 'cancelled': 0}
        self.server.harness = MagicMock()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, path, body=None, **headers):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={'Authorization': 'Bearer fixture', 'Content-Type': 'application/json', **headers})
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def test_only_claude_code_model_is_advertised(self):
        status, body = self.request('GET', '/v1/models')
        self.assertEqual(status, 200)
        self.assertEqual([item['id'] for item in body['data']], [HARNESS_MODEL])
        _, health = self.request('GET', '/health')
        self.assertEqual(health['harness'], 'claude-code')

    def test_removed_model_rejected_without_starting_harness(self):
        for model in ('claude-opus-5-5', 'claude-sonnet-5'):
            status, body = self.request('POST', '/v1/responses', {'model': model, 'stream': True})
            self.assertEqual(status, 400)
            self.assertIn(HARNESS_MODEL, body['error']['message'])
        self.server.harness.handle.assert_not_called()

    def test_nonstreaming_and_missing_metadata_rejected_before_sse(self):
        for body in ({'model': HARNESS_MODEL}, {'model': HARNESS_MODEL, 'stream': True}):
            self.assertEqual(self.request('POST', '/v1/responses', body)[0], 400)
        self.server.harness.handle.assert_not_called()

    def test_origin_and_wrong_auth_are_rejected(self):
        self.assertEqual(self.request('GET', '/health', Origin='https://example.invalid')[0], 401)
        self.assertEqual(self.request('GET', '/health', Authorization='Bearer wrong')[0], 401)


class CarrierTests(unittest.TestCase):
    def test_old_history_carriers_still_decode(self):
        codec = CarrierCodec(Fernet.generate_key())
        for value in ({'type': 'codex_compaction', 'summary': 'previous work'}, {'type': 'claude_code', 'thread': 'old'}):
            encrypted = codec.encode(value)
            self.assertTrue(encrypted.startswith('codex_claude_v1.'))
            self.assertEqual(codec.decode(encrypted), value)
        self.assertIsNone(codec.decode('foreign_opaque_reasoning'))
        with self.assertRaises(ProtocolError):
            codec.decode('codex_claude_v1.invalid')


if __name__ == '__main__':
    unittest.main()
