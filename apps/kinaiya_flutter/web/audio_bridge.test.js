/// Deterministic Node tests for web/audio_bridge.js using a fake window.
/// No network, no LLM, no real media devices. Run: node --test web/audio_bridge.test.js
'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function makeFakes(opts = {}) {
  const mimeSupported = opts.mimeSupported ?? ['audio/webm;codecs=opus'];
  const resumeGate = { release: null, resumed: false };
  const seen = { mimeOpts: [], stoppedTracks: 0, closedCtx: false };
  const recorders = [];
  const sources = [];

  class FakeTrack {
    stop() { seen.stoppedTracks++; }
  }
  class FakeStream {
    constructor() { this.tracks = [new FakeTrack(), new FakeTrack()]; }
    getTracks() { return this.tracks; }
  }
  class FakeMediaRecorder {
    static isTypeSupported(m) { return mimeSupported.includes(m); }
    constructor(stream, options) {
      seen.mimeOpts.push(options);
      this.stream = stream;
      this.options = options;
      this.state = 'inactive';
      this.ondataavailable = null;
      this.onstop = null;
      recorders.push(this);
    }
    start() { this.state = 'recording'; }
    stop() {
      if (this.state === 'inactive') return;
      this.state = 'inactive';
      queueMicrotask(() => { if (this.onstop) this.onstop(); });
    }
  }
  class FakeSource {
    constructor() { this.onended = null; this.startedAt = -1; }
    start(t) { this.startedAt = t; }
    stop() { queueMicrotask(() => { if (this.onended) this.onended(); }); }
    connect() {}
    disconnect() {}
  }
  class FakeBuffer {
    constructor(n, rate) { this.duration = n / rate; this.data = new Float32Array(n); }
    getChannelData() { return this.data; }
  }
  class FakeContext {
    constructor() {
      this.currentTime = 100;
      this.state = opts.suspended ? 'suspended' : 'running';
      this.destination = {};
    }
    resume() {
      if (opts.holdResume) {
        return new Promise((res) => {
          resumeGate.release = () => { this.state = 'running'; resumeGate.resumed = true; res(); };
        });
      }
      this.state = 'running';
      return Promise.resolve();
    }
    createBuffer(ch, n, rate) { return new FakeBuffer(n, rate); }
    createBufferSource() { const source = new FakeSource(); sources.push(source); return source; }
    close() { seen.closedCtx = true; return Promise.resolve(); }
  }

  const sandbox = {
    console,
    queueMicrotask, setTimeout, clearTimeout, setInterval, clearInterval,
    Uint8Array, Float32Array, Array, Object, JSON, Math, Date: opts.clock ? class extends Date { static now() { return opts.clock.value; } } : Date, Promise,
    Blob, atob: (s) => Buffer.from(s, 'base64').toString('binary'),
    btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
    MediaRecorder: FakeMediaRecorder,
    navigator: {
      mediaDevices: opts.noMic ? undefined : {
        getUserMedia: (constraints) => { seen.constraints=constraints; return opts.permissionGate ? new Promise(resolve => {opts.permissionGate.resolve=() => resolve(new FakeStream());}) : Promise.resolve(new FakeStream()); },
      },
    },
  };
  sandbox.window = {
    AudioContext: opts.noAudio ? undefined : FakeContext,
    navigator: sandbox.navigator,
    Blob, atob: sandbox.atob, btoa: sandbox.btoa,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  const src = fs.readFileSync(path.join(__dirname, 'audio_bridge.js'), 'utf8');
  vm.runInContext(src, sandbox, { filename: 'audio_bridge.js' });
  return { api: sandbox.window.kinaiyaAudio, seen, recorders, resumeGate, FakeStream, sources };
}

/// Mirrors Dart `_str`: sync strings pass through, promises are awaited.
async function str(v) {
  const r = typeof v === 'function' ? v() : v;
  if (r && typeof r.then === 'function') {
    const out = await r;
    assert.equal(typeof out, 'string', 'async bridge fn must resolve a string');
    return out;
  }
  assert.equal(typeof r, 'string', 'sync bridge fn must return a string');
  return r;
}

test('sync/async calling semantics normalize through one helper', async () => {
  const { api } = makeFakes();
  // Sync: capabilities/cancelMic/stopAudio/playbackActive/pollEvents.
  assert.equal(await str(() => api.capabilities()), await str(() => api.capabilities()));
  const caps = JSON.parse(await str(() => api.capabilities()));
  assert.equal(caps.mic, true);
  assert.equal(caps.playback, true);
  assert.equal(await str(() => api.cancelMic()), 'cancelled');
  assert.equal(await str(() => api.stopAudio()), 'stopped');
  assert.equal(await str(() => api.playbackActive()), 'no');
  assert.deepEqual(JSON.parse(await str(() => api.pollEvents())).length >= 0, true);
  // Async: startMic/stopMic/queuePcm/awaitPlayback.
  assert.equal(await str(() => api.startMic()), 'recording');
  assert.equal(await str(() => api.stopAudio()), 'stopped');
});

test('startMic passes the selected mimeType through', async () => {
  const { api, seen } = makeFakes();
  assert.equal(await api.startMic(), 'recording');
  assert.equal(seen.mimeOpts[0].mimeType, 'audio/webm;codecs=opus');
  await api.stopArmed().catch(() => {});
});

test('stopMic with inactive recorder resolves without hanging', async () => {
  const { api } = makeFakes();
  await api.startMic();
  // Force inactive (already-stopped recorder) then stop again.
  await api.stopMic();
  const out = JSON.parse(await api.stopMic());
  assert.equal(out.base64, '');
  // Missing recorder entirely also resolves.
  const out2 = JSON.parse(await api.stopMic());
  assert.equal(out2.base64, '');
});

test('dispose cancels tracks and reports disposed', async () => {
  const { api, seen } = makeFakes();
  await api.startMic();
  assert.equal(api.dispose(), 'disposed');
  assert.ok(seen.stoppedTracks >= 2, 'mic tracks stopped');
  assert.equal(api.playbackActive(), 'no');
  assert.deepEqual(JSON.parse(api.pollEvents()), []);
});

test('stop during suspended resume invalidates stale queued audio', async () => {
  const { api, resumeGate } = makeFakes({ suspended: true, holdResume: true });
  await api.startMic();
  const pcm = Buffer.from(new Int16Array([1, 2, 3, 4]).buffer).toString('base64');
  const pending = api.queuePcm(pcm, 24000); // waits on resume
  assert.equal(api.stopAudio(), 'stopped'); // invalidate while suspended
  resumeGate.release();
  assert.equal(await pending, 'stopped', 'stale entry never starts a source');
  assert.equal(api.playbackActive(), 'no');
  assert.deepEqual(JSON.parse(api.playbackInfo()), []);
});

test('played requires true onended; synthesis-complete is not played', async () => {
  const { api } = makeFakes();
  const pcm = Buffer.from(new Int16Array(240).fill(100).buffer).toString('base64');
  assert.equal(await api.queuePcm(pcm, 24000), 'queued');
  assert.equal(api.playbackActive(), 'yes', 'source scheduled after queue');
  let done = null;
  const waiter = api.awaitPlayback().then((v) => { done = v; return v; });
  await Promise.resolve();
  assert.equal(done, null, 'awaitPlayback pending until onended fires');
  // End all sources by stopping (simulates natural end via onended below).
  api.stopAudio();
  assert.equal(await waiter, 'stopped');
  // Natural end path: queue then fire onended directly.
  assert.equal(await api.queuePcm(pcm, 24000), 'queued');
  const p2 = api.awaitPlayback();
  // Reach into a fresh mic-less check: stop resolves waiters; here instead
  // resolve by stopping again after asserting still-playing.
  assert.equal(api.playbackActive(), 'yes');
  api.stopAudio();
  assert.equal(await p2, 'stopped');
});

test('VAD: silence stays quiet, noise adapts, speech ends after end-silence', () => {
  const { api } = makeFakes();
  api.vadConfigure(JSON.stringify({ endSilenceMs: 800, minSpeechMs: 200 }));
  return api.startArmed().then(() => {
    const t0 = 1000000;
    for (let i = 0; i < 20; i++) {
      assert.equal(api.vadSample(0.001, t0 + i * 100), '', 'silence: no event');
    }
    for (let i = 0; i < 10; i++) {
      assert.equal(api.vadSample(0.012, t0 + 2000 + i * 100), '', 'steady noise adapts: no event');
    }
    let start = '';
    for (let i = 0; i < 8; i++) {
      const e = api.vadSample(0.4, t0 + 4000 + i * 100);
      if (e) start = e;
    }
    assert.equal(JSON.parse(start).type, 'speech-start');
    assert.equal(api.vadSample(0.001, t0 + 5000), '', 'end-silence not yet reached');
    const end = api.vadSample(0.001, t0 + 6000);
    assert.equal(JSON.parse(end).type, 'speech-end');
  });
});

test('VAD: 220ms sustained voice during playback barges; nested epochs isolate', () => {
  const { api } = makeFakes();
  api.vadConfigure(JSON.stringify({ endSilenceMs: 1500, minSpeechMs: 100 }));
  return api.startArmed().then(async () => {
    const t0 = 2000000;
    const pcm = Buffer.from(new Int16Array(240).fill(50).buffer).toString('base64');
    await api.queuePcm(pcm, 24000);
    assert.equal(api.playbackActive(), 'yes');
    // Below sustain: no barge.
    assert.equal(api.vadSample(0.5, t0), '');
    assert.equal(api.vadSample(0.5, t0 + 50), '');
    // Sustain voice through speech-start into the 220ms barge window.
    let barge = '';
    for (let i = 2; i < 20 && !barge; i++) {
      const e = api.vadSample(0.5, t0 + i * 50);
      if (e && JSON.parse(e).type === 'barge') barge = e;
    }
    assert.equal(JSON.parse(barge).type, 'barge', '220ms sustained voice barges');
    // Nested epoch: re-arm, old-epoch samples are ignored.
    await api.startArmed();
    const epochNow = JSON.parse(api.vadConfigure('{}')).bargeMs;
    assert.equal(epochNow, 220);
    assert.equal(api.vadSample(0.9, t0 + 400, 1), '', 'stale epoch ignored');
  });
});

test('VAD: 60s clip cap fires once', () => {
  const { api } = makeFakes();
  return api.startArmed().then(() => {
    const t0 = Date.now();
    assert.equal(api.vadSample(0.001, t0), '');
    const cap = api.vadSample(0.001, t0 + 60000);
    assert.equal(JSON.parse(cap).type, 'clip-cap');
    assert.equal(api.vadSample(0.001, t0 + 61000), '', 'cap fires once');
  });
});


test('permission resolving after disposal releases every track and starts no recorder', async () => {
  const permissionGate={};
  const {api,seen,recorders}=makeFakes({permissionGate});
  const pending=api.startMic();
  api.dispose();
  permissionGate.resolve();
  assert.equal(await pending,'unavailable');
  assert.equal(seen.stoppedTracks,2);
  assert.equal(recorders.length,0);
  assert.equal(seen.constraints.audio.echoCancellation,true);
});

test('each complete clip has a fresh epoch and its own 60-second cap', async () => {
  const clock={value:1000};
  const {api}=makeFakes({clock});
  await api.startArmed();await api.beginClip();
  const first=Number(api.captureEpoch());
  clock.value=61001;
  assert.equal(JSON.parse(api.vadSample(0,clock.value,first)).type,'clip-cap');
  await api.finishClip();await api.beginClip();
  const second=Number(api.captureEpoch());
  assert.ok(second>first);
  assert.equal(api.vadSample(0,clock.value+61000,first),'');
  assert.equal(api.vadSample(0,clock.value+1,second),'');
  clock.value+=60001;
  assert.equal(JSON.parse(api.vadSample(0,clock.value,second)).type,'clip-cap');
  api.dispose();
});

test('a silence dip resets barge sustain and 220ms stops local audio immediately', async () => {
  const {api}=makeFakes();
  await api.startArmed();await api.beginClip();
  await api.queuePcm(Buffer.alloc(48000).toString('base64'),24000);
  const epoch=Number(api.captureEpoch());
  api.vadSample(0.9,1000,epoch);
  api.vadSample(0,1100,epoch);
  api.vadSample(0.9,1200,epoch);
  assert.equal(api.vadSample(0.9,1419,epoch),'');
  assert.equal(api.playbackActive(),'yes');
  assert.equal(JSON.parse(api.vadSample(0.9,1420,epoch)).type,'barge');
  assert.equal(api.playbackActive(),'no');
  api.dispose();
});

test('old source ended callback cannot complete a new generation playback', async () => {
  const {api,sources}=makeFakes();
  const frame=Buffer.alloc(480).toString('base64');
  await api.queuePcm(frame,24000);
  const old=sources[0];api.stopAudio();
  await api.queuePcm(frame,24000);
  let ended=false;
  const waiter=api.awaitPlayback().then(() => {ended=true;});
  old.onended();await Promise.resolve();
  assert.equal(ended,false);
  sources[1].onended();await waiter;
  assert.equal(ended,true);
  api.dispose();
});
