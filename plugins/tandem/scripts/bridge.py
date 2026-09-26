"""Authenticated loopback Responses endpoint for the Claude Code harness."""
from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from harness import HARNESS_MODEL, Harness, thread_id
from responses import CarrierCodec, ProtocolError

ROOT = Path(__file__).resolve().parents[1]
CODEX_HOME = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
# Private state lives outside the plugin so it survives plugin updates.
STATE = Path(os.environ.get('TANDEM_HOME') or CODEX_HOME / 'tandem')
PORT = 57855
MAX_BODY = 32 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def send_json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= MAX_BODY:
            raise ProtocolError('Invalid request size')
        body = json.loads(self.rfile.read(length))
        if not isinstance(body, dict):
            raise ProtocolError('Request body must be an object')
        return body

    def authorized(self):
        if self.headers.get('Origin') or not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.secret):
            self.send_json(401, {'error': {'message': 'Local bridge authentication required', 'type': 'authentication_error'}})
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        if self.path == '/health':
            with self.server.lock:
                metrics = dict(self.server.metrics)
            self.send_json(200, {'bridge': 'tandem', 'model': HARNESS_MODEL,
                                'harness': 'claude-code', 'pid': os.getpid(), **metrics})
        elif self.path == '/v1/models':
            self.send_json(200, {'object': 'list', 'data': [{'id': HARNESS_MODEL, 'object': 'model', 'owned_by': 'anthropic'}]})
        else:
            self.send_json(404, {'error': {'message': 'Unknown endpoint'}})

    def peer_closed(self):
        readable, _, _ = select.select([self.connection], [], [], 0)
        return bool(readable) and not self.connection.recv(1, socket.MSG_PEEK)

    def send_event(self, event):
        self.wfile.write(('event: ' + event['type'] + '\ndata: ' + json.dumps(event, ensure_ascii=False) + '\n\n').encode('utf-8'))
        self.wfile.flush()

    def relay(self):
        token = self.headers.get('Authorization', '').removeprefix('Bearer ')
        session = self.server.harness.by_token(token) if token else None
        if self.headers.get('Origin') or session is None:
            self.send_json(401, {'error': {'message': 'Unknown relay session'}})
            return
        try:
            body = self.read_body()
        except (ValueError, TypeError) as error:
            self.send_json(400, {'error': {'message': str(error)}})
            return
        self.send_json(200, session.relay(body.get('name'), body.get('arguments') or {}, body.get('tool_use_id')))

    def do_POST(self):
        if self.path == '/relay':
            self.relay()
            return
        if not self.authorized():
            return
        if self.path == '/shutdown':
            self.send_json(200, {'stopping': True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if self.path != '/v1/responses':
            self.send_json(400, {'error': {'message': 'Only /v1/responses is supported'}})
            return
        try:
            body = self.read_body()
            if body.get('model') != HARNESS_MODEL:
                raise ProtocolError('Only claude-opus-5-5-claude-code is supported. Select Claude Opus 5.5 (Claude Code).')
            if not body.get('stream'):
                raise ProtocolError('The Claude Code harness requires streaming')
            if not thread_id(body):
                raise ProtocolError('Codex thread metadata is missing')
        except (ValueError, KeyError, TypeError) as error:
            self.send_json(400, {'error': {'message': str(error), 'type': 'invalid_request_error'}})
            return

        handler = self

        class IO:
            @staticmethod
            def emit(event):
                handler.send_event(event)

            @staticmethod
            def check():
                if handler.peer_closed():
                    raise ConnectionAbortedError('Client disconnected')

        with self.server.lock:
            self.server.metrics['harness_requests'] += 1
            self.server.metrics['active_requests'] += 1
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.server.harness.handle(body, IO)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            with self.server.lock:
                self.server.metrics['cancelled'] += 1
        finally:
            with self.server.lock:
                self.server.metrics['active_requests'] -= 1


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args()
    settings = json.loads((STATE / 'settings.json').read_text())
    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    server.secret = settings['secret']
    server.codec = CarrierCodec(settings['carrier_key'].encode())
    server.lock = threading.Lock()
    server.metrics = {'harness_requests': 0, 'active_requests': 0, 'cancelled': 0}
    server.harness = Harness(server.codec, STATE, args.port)
    (STATE / 'bridge.pid').write_text(str(os.getpid()))
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        server.harness.close()
        server.server_close()
        (STATE / 'bridge.pid').unlink(missing_ok=True)


if __name__ == '__main__':
    run()
