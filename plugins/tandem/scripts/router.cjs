// Local model routing. GPT payloads and native authentication stay unchanged.
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const https = require('node:https');
const path = require('node:path');
const zlib = require('node:zlib');

const PORT = 57856;
const MODEL = 'claude-opus-5-5-claude-code';
const UPSTREAM = 'https://chatgpt.com/backend-api/codex';
const MAX_BODY = 256 * 1024 * 1024;
const CARRIER_PREFIX = 'codex_claude_v1.';
// Only Responses requests carry a model choice; every other native route is passed through untouched.
const MODEL_ROUTE = /^\/responses(?:\/compact)?(?:\?|$)/;

function decodeBody(bytes, encoding) {
  if (!encoding || encoding === 'identity') return bytes;
  if (encoding === 'gzip') return zlib.gunzipSync(bytes, {maxOutputLength: MAX_BODY});
  if (encoding === 'deflate') return zlib.inflateSync(bytes, {maxOutputLength: MAX_BODY});
  if (encoding === 'zstd') return zlib.zstdDecompressSync(bytes, {maxOutputLength: MAX_BODY});
  throw new Error('Unsupported request content encoding');
}

function headersFor(headers) {
  return Object.fromEntries(Object.entries(headers).filter(([key]) => !['host', 'connection', 'transfer-encoding', 'proxy-authorization', 'proxy-connection'].includes(key.toLowerCase())));
}

// Fernet (Python cryptography) decryption for the bridge's local compaction carrier.
function fernetDecrypt(key, token) {
  const secret = Buffer.from(key, 'base64url');
  const data = Buffer.from(token, 'base64url');
  if (secret.length !== 32 || data.length < 57 || data[0] !== 0x80) throw new Error('Malformed carrier');
  const signed = data.subarray(0, data.length - 32);
  const mac = crypto.createHmac('sha256', secret.subarray(0, 16)).update(signed).digest();
  if (!crypto.timingSafeEqual(mac, data.subarray(data.length - 32))) throw new Error('Carrier signature mismatch');
  const decipher = crypto.createDecipheriv('aes-128-cbc', secret.subarray(16), data.subarray(9, 25));
  return Buffer.concat([decipher.update(data.subarray(25, data.length - 32)), decipher.final()]).toString('utf8');
}

// OpenAI cannot verify the bridge's encrypted Opus items, so a chat switched from Opus to GPT
// drops Opus reasoning carriers and replays Opus compaction as a plain summary message.
function stripOpusArtifacts(body, carrierKey) {
  if (!Array.isArray(body?.input)) return false;
  let changed = false;
  const input = [];
  for (const item of body.input) {
    if (typeof item?.encrypted_content !== 'string' || !item.encrypted_content.startsWith(CARRIER_PREFIX)) {
      input.push(item);
      continue;
    }
    changed = true;
    if (item.type !== 'compaction') continue;
    let summary;
    try {
      summary = JSON.parse(fernetDecrypt(carrierKey, item.encrypted_content.slice(CARRIER_PREFIX.length)));
    } catch {
      throw new Error('This chat contains an Opus compaction summary that could not be decoded; start a new GPT chat');
    }
    if (summary?.type !== 'codex_compaction') throw new Error('Unexpected Opus compaction carrier');
    input.push({type: 'message', role: 'user', content: [{type: 'input_text', text: 'Earlier conversation summary:\n' + summary.summary}]});
  }
  if (changed) body.input = input;
  return changed;
}

function createRouter(settings, upstream = UPSTREAM, claudeEndpoint = 'http://127.0.0.1:57855/v1') {
  // The upstream parameter is an in-process test seam. The production launcher
  // always uses the fixed official ChatGPT endpoint; no CLI/env override exists.
  const prefix = '/' + settings.secret + '/v1';
  const bearer = Buffer.from('Bearer ' + settings.secret);
  const metrics = {gpt_requests: 0, opus_requests: 0, passthrough_requests: 0, rejected_models: 0, opus_artifacts_stripped: 0, websocket_fallbacks: 0, websocket_tunnels: 0};
  const connections = new Set();
  const authorized = req => {
    const given = Buffer.from(req.headers.authorization || '');
    return given.length === bearer.length && crypto.timingSafeEqual(given, bearer);
  };
  const routeOf = url => url.startsWith(prefix + '/') ? url.slice(prefix.length) : null;
  const json = (res, status, value) => {
    const bytes = Buffer.from(JSON.stringify(value));
    res.writeHead(status, {'Content-Type': 'application/json', 'Content-Length': bytes.length});
    res.end(bytes);
  };
  const forward = (req, res, target, headers, payload) => {
    const transport = target.protocol === 'https:' ? https : http;
    const outgoing = transport.request(target, {method: req.method, headers}, incoming => {
      res.writeHead(incoming.statusCode, headersFor(incoming.headers));
      incoming.pipe(res);
      incoming.on('error', () => res.destroy());
    });
    connections.add(outgoing);
    outgoing.on('close', () => connections.delete(outgoing));
    outgoing.on('error', () => {
      if (!res.headersSent) json(res, 502, {error: {message: 'The selected model endpoint could not be reached'}});
      else res.destroy();
    });
    res.on('close', () => outgoing.destroy());
    if (payload === undefined) {
      req.pipe(outgoing);
      req.on('error', () => outgoing.destroy());
    } else {
      outgoing.end(payload.length ? payload : undefined);
    }
  };
  const handle = async (req, res) => {
    if (req.headers.origin) return json(res, 403, {error: {message: 'Browser-origin requests are disabled'}});
    if (req.url === '/health' && authorized(req)) {
      return json(res, 200, {router: 'tandem', pid: process.pid, ...metrics});
    }
    if (req.url === '/shutdown' && req.method === 'POST' && authorized(req)) {
      json(res, 200, {stopping: true});
      for (const connection of connections) connection.destroy();
      return server.close();
    }
    const route = routeOf(req.url);
    if (route === null) return json(res, 404, {error: {message: 'Unknown route'}});
    if (!MODEL_ROUTE.test(route)) {
      // Realtime calls, search, models, memories, ...: native payloads (not always JSON) stream through as-is.
      metrics.passthrough_requests++;
      return forward(req, res, new URL(upstream + route), headersFor(req.headers));
    }
    let raw, body;
    try {
      const chunks = [];
      let size = 0;
      for await (const chunk of req) {
        size += chunk.length;
        if (size > MAX_BODY) throw new Error('Request exceeds local size limit');
        chunks.push(chunk);
      }
      raw = Buffer.concat(chunks);
      if (raw.length) body = JSON.parse(decodeBody(raw, req.headers['content-encoding']).toString());
    } catch (error) {
      return json(res, 400, {error: {message: error.message}});
    }
    const model = typeof body?.model === 'string' ? body.model : undefined;
    if (model === MODEL) {
      metrics.opus_requests++;
      // OpenAI credentials and host metadata never reach Claude Code/Anthropic.
      const payload = Buffer.from(JSON.stringify(body));
      return forward(req, res, new URL(claudeEndpoint + route), {'Authorization': 'Bearer ' + settings.secret, 'Content-Type': 'application/json', 'Content-Length': payload.length}, payload);
    }
    if (model?.startsWith('claude-')) {
      metrics.rejected_models++;
      return json(res, 400, {error: {message: 'Only GPT/Codex models and Claude Opus 5.5 (Claude Code) are supported. Select claude-opus-5-5-claude-code.'}});
    }
    metrics.gpt_requests++;
    const headers = headersFor(req.headers);
    let payload = raw;
    try {
      if (stripOpusArtifacts(body, settings.carrier_key)) {
        metrics.opus_artifacts_stripped++;
        payload = Buffer.from(JSON.stringify(body));
        delete headers['content-encoding'];
      }
    } catch (error) {
      return json(res, 400, {error: {message: error.message}});
    }
    delete headers['content-length'];
    if (payload.length) headers['Content-Length'] = payload.length;
    return forward(req, res, new URL(upstream + route), headers, payload);
  };
  const server = http.createServer((req, res) => {
    handle(req, res).catch(() => {
      if (!res.headersSent) json(res, 500, {error: {message: 'Local router failure'}});
      else res.destroy();
    });
  });
  server.on('upgrade', (req, socket, head) => {
    socket.on('error', () => {});
    const route = routeOf(req.url);
    // Codex's supported transport fallback preserves the native HTTP Responses protocol, which
    // carries the model choice. Other native WebSockets are tunnelled to the fixed upstream.
    if (req.headers.origin || route === null || MODEL_ROUTE.test(route)) {
      metrics.websocket_fallbacks++;
      return socket.end('HTTP/1.1 426 Upgrade Required\r\nContent-Length: 0\r\nConnection: close\r\n\r\n');
    }
    metrics.websocket_tunnels++;
    const target = new URL(upstream + route);
    const transport = target.protocol === 'https:' ? https : http;
    const outgoing = transport.request(target, {method: req.method, headers: {...headersFor(req.headers), connection: 'Upgrade'}});
    const statusLine = incoming => {
      let text = `HTTP/1.1 ${incoming.statusCode} ${incoming.statusMessage}\r\n`;
      for (let i = 0; i < incoming.rawHeaders.length; i += 2) text += `${incoming.rawHeaders[i]}: ${incoming.rawHeaders[i + 1]}\r\n`;
      return text + '\r\n';
    };
    outgoing.on('upgrade', (incoming, upstreamSocket, upstreamHead) => {
      // HTTP server sockets allow half-open connections; tear down both sides when either ends.
      const teardown = () => {
        socket.destroy();
        upstreamSocket.destroy();
      };
      for (const side of [socket, upstreamSocket]) {
        side.on('error', teardown);
        side.on('end', teardown);
        side.on('close', teardown);
      }
      socket.write(statusLine(incoming));
      if (upstreamHead.length) socket.write(upstreamHead);
      upstreamSocket.pipe(socket).pipe(upstreamSocket);
    });
    outgoing.on('response', incoming => {
      socket.write(statusLine(incoming));
      incoming.pipe(socket);
    });
    outgoing.on('error', () => socket.destroy());
    socket.on('close', () => outgoing.destroy());
    if (head.length) outgoing.write(head);
    outgoing.end();
  });
  return {server, metrics, prefix};
}

if (require.main === module) {
  // The launcher passes Tandem's private state directory.
  const STATE = process.argv[2];
  const settings = JSON.parse(fs.readFileSync(path.join(STATE, 'settings.json'), 'utf8'));
  const {server} = createRouter(settings);
  server.listen(PORT, '127.0.0.1', () => fs.writeFileSync(path.join(STATE, 'router.pid'), String(process.pid)));
  server.on('close', () => {
    fs.rmSync(path.join(STATE, 'router.pid'), {force: true});
    process.exit(0);
  });
}

module.exports = {createRouter, decodeBody, fernetDecrypt, stripOpusArtifacts};
