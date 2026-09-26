const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const zlib = require('node:zlib');
const {createRouter} = require('../scripts/router.cjs');

test('GPT headers and compressed body are preserved; Opus receives only local auth', async t => {
  const receipts = [];
  const mock = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    receipts.push({route: req.url, headers: req.headers, body: Buffer.concat(chunks)});
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end('{"fixture":true}');
  });
  await new Promise(resolve => mock.listen(0, '127.0.0.1', resolve));
  const target = `http://127.0.0.1:${mock.address().port}`;
  const {server, prefix} = createRouter({secret:'test-route-secret'}, target + '/gpt', target + '/claude');
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => {server.closeAllConnections(); server.close(); mock.closeAllConnections(); mock.close();});
  async function send(body, headers={}) {
    return new Promise((resolve,reject) => {
      const req = http.request({hostname:'127.0.0.1',port:server.address().port,path:prefix+'/responses',method:'POST',headers:{'Content-Type':'application/json','Content-Length':body.length,...headers}}, res => {
        res.resume(); res.on('end',()=>resolve(res.statusCode));
      });
      req.on('error',reject);req.end(body);
    });
  }
  const native = zlib.zstdCompressSync(Buffer.from('{"model":"gpt-6-sol","input":"fixture"}'));
  assert.equal(await send(native, {'Authorization':'Bearer native-fixture-token','ChatGPT-Account-Id':'account-fixture','X-OAI-Attestation':'attestation-fixture','Content-Encoding':'zstd'}),200);
  assert.deepEqual(receipts[0].body,native);
  assert.equal(receipts[0].route,'/gpt/responses');
  assert.equal(receipts[0].headers.authorization,'Bearer native-fixture-token');
  assert.equal(receipts[0].headers['x-oai-attestation'],'attestation-fixture');
  assert.equal(await send(Buffer.from('{"model":"claude-opus-5-5-claude-code","input":"fixture"}'),{'Authorization':'Bearer native-fixture-token','ChatGPT-Account-Id':'account-fixture','X-OAI-Attestation':'attestation-fixture'}),200);
  assert.equal(receipts[1].route,'/claude/responses');
  assert.equal(receipts[1].headers.authorization,'Bearer test-route-secret');
  assert.equal(receipts[1].headers['chatgpt-account-id'],undefined);
  assert.equal(receipts[1].headers['x-oai-attestation'],undefined);
  assert.equal(await send(Buffer.from('{"model":"claude-sonnet-5"}')),400);
  assert.equal(receipts.length,2);
});

async function fixture(t) {
  const receipts = [];
  const mock = http.createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    receipts.push({route: req.url, headers: req.headers, body: Buffer.concat(chunks)});
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end('{"fixture":true}');
  });
  mock.on('upgrade', (req, socket) => {
    receipts.push({route: req.url, upgrade: req.headers.upgrade});
    socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n');
    socket.on('data', data => socket.write(Buffer.concat([Buffer.from('echo:'), data])));
  });
  await new Promise(resolve => mock.listen(0, '127.0.0.1', resolve));
  const target = `http://127.0.0.1:${mock.address().port}`;
  const key = require('node:crypto').randomBytes(32).toString('base64url') + '=';
  const router = createRouter({secret: 'test-route-secret', carrier_key: key}, target + '/gpt', target + '/claude');
  await new Promise(resolve => router.server.listen(0, '127.0.0.1', resolve));
  t.after(() => {router.server.closeAllConnections(); router.server.close(); mock.closeAllConnections(); mock.close();});
  const send = (route, body, headers = {}) => new Promise((resolve, reject) => {
    const req = http.request({hostname: '127.0.0.1', port: router.server.address().port, path: router.prefix + route, method: 'POST', headers: {'Content-Length': body.length, ...headers}}, res => {
      res.resume(); res.on('end', () => resolve(res.statusCode));
    });
    req.on('error', reject); req.end(body);
  });
  return {receipts, send, key, router};
}

test('non-Responses native routes pass through unparsed', async t => {
  const {receipts, send} = await fixture(t);
  const sdp = Buffer.from('v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n');
  assert.equal(await send('/realtime/calls?intent=quicksilver', sdp, {'Content-Type': 'application/sdp', 'Authorization': 'Bearer native'}), 200);
  assert.equal(receipts[0].route, '/gpt/realtime/calls?intent=quicksilver');
  assert.deepEqual(receipts[0].body, sdp);
  assert.equal(receipts[0].headers.authorization, 'Bearer native');
  assert.equal(await send('/alpha/search', Buffer.from('{"q":"x"}')), 200);
  assert.equal(receipts[1].route, '/gpt/alpha/search');
});

test('future GPT slugs are forwarded; other Claude models are rejected', async t => {
  const {receipts, send} = await fixture(t);
  assert.equal(await send('/responses', Buffer.from('{"model":"o9-preview"}')), 200);
  assert.equal(receipts[0].route, '/gpt/responses');
  assert.equal(await send('/responses', Buffer.from('{"model":"claude-sonnet-5"}')), 400);
  assert.equal(await send('/responses', Buffer.from('{"model":"claude-opus-5-5"}')), 400);
  assert.equal(receipts.length, 1);
});

test('Opus carriers are removed before a GPT request and Opus compaction becomes a summary', async t => {
  const {receipts, send, key} = await fixture(t);
  const {execFileSync} = require('node:child_process');
  // Encrypt with the real Python Fernet implementation used by the bridge.
  const python = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  const encrypt = value => 'codex_claude_v1.' + execFileSync(python, ['-c', 'import sys;from cryptography.fernet import Fernet;print(Fernet(sys.argv[1].encode()).encrypt(sys.argv[2].encode()).decode())', key, JSON.stringify(value)]).toString().trim();
  const body = {model: 'gpt-6-sol', input: [
    {type: 'compaction', encrypted_content: encrypt({type: 'codex_compaction', summary: 'earlier work'})},
    {type: 'reasoning', encrypted_content: encrypt({type: 'carrier'})},
    {type: 'reasoning', encrypted_content: 'gAAAA-native-openai'},
    {role: 'user', content: 'next'},
  ]};
  const compressed = zlib.zstdCompressSync(Buffer.from(JSON.stringify(body)));
  assert.equal(await send('/responses', compressed, {'Content-Encoding': 'zstd', 'Content-Type': 'application/json'}), 200);
  const forwarded = JSON.parse(receipts[0].body.toString());
  assert.equal(receipts[0].headers['content-encoding'], undefined);
  assert.equal(forwarded.input.length, 3);
  assert.equal(forwarded.input[0].content[0].text, 'Earlier conversation summary:\nearlier work');
  assert.equal(forwarded.input[1].encrypted_content, 'gAAAA-native-openai');
  const tampered = {model: 'gpt-6-sol', input: [{type: 'compaction', encrypted_content: 'codex_claude_v1.gAAAAAtampered'}]};
  assert.equal(await send('/responses', Buffer.from(JSON.stringify(tampered))), 400);
  assert.equal(receipts.length, 1);
});

test('Responses WebSockets fall back to HTTP; other WebSockets are tunnelled', async t => {
  const {receipts, router} = await fixture(t);
  const net = require('node:net');
  const upgrade = route => new Promise((resolve, reject) => {
    const socket = net.connect(router.server.address().port, '127.0.0.1', () => {
      socket.write(`GET ${router.prefix}${route} HTTP/1.1\r\nHost: x\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n`);
    });
    let data = '';
    socket.on('data', chunk => {
      data += chunk;
      if (data.startsWith('HTTP/1.1 101') && data.includes('\r\n\r\n') && !data.includes('echo:')) socket.write('ping');
      if (data.includes('echo:ping') || data.startsWith('HTTP/1.1 426')) { socket.destroy(); resolve(data); }
    });
    socket.on('error', reject);
  });
  assert.match(await upgrade('/responses'), /^HTTP\/1\.1 426/);
  assert.match(await upgrade('/realtime'), /echo:ping/);
  assert.equal(receipts[0].route, '/gpt/realtime');
});
