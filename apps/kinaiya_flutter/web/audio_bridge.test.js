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
      seen.context = this;
      this.currentTime = 100;
      this.state = opts.suspended ? 'suspended' : 'running';
      this.destination = {};
      if (opts.windowCapture) {
        this.sampleRate = 1000;
        this.createMediaStreamSource = () => new FakeSource();
        this.createGain = () => ({gain: {value: 1}, connect() {}, disconnect() {}});
        this.createScriptProcessor = () => {
          const node = {onaudioprocess:null,connect(){},disconnect(){seen.captureDisconnected=true;}};
          seen.processor=node;
          return node;
        };
      }
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
    Uint8Array,
    Float32Array: opts.trackEnvelopes ? class extends Float32Array {
      constructor(length) {
        super(length);
        (seen.envelopeSizes ??= []).push(this.length);
      }
    } : Float32Array,
    Array, Object, JSON, Math, Date: opts.clock ? class extends Date { static now() { return opts.clock.value; } } : Date, Promise,
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

test('semantic capture keeps rolling PCM and leaves timing to native decisions', async () => {
  const {api,seen,recorders}=makeFakes({windowCapture:true});
  api.windowConfigure(true);
  assert.equal(seen.constraints,undefined,'configuration never opens the mic');
  assert.equal(await api.startArmed(),'armed');
  assert.equal(recorders.length,0,'streaming path does not wait for MediaRecorder clips');
  const epoch=Number(api.nextWindowUtterance());
  seen.processor.onaudioprocess({inputBuffer:{getChannelData:()=>new Float32Array([0,0.5,-0.5,1])}});
  api.vadSample(0.2,1000);
  const snapshot=JSON.parse(api.windowSnapshot());
  assert.equal(snapshot.capture_epoch,epoch);
  assert.equal(snapshot.user_speaking,true);
  assert.equal(snapshot.end_ms,4);
  const wav=Buffer.from(snapshot.base64,'base64');
  assert.equal(wav.toString('ascii',0,4),'RIFF');
  assert.equal(wav.readUInt32LE(24),1000);
  assert.equal(wav.readInt16LE(46),16383);
  await api.queuePcm(Buffer.alloc(200).toString('base64'),1000);
  api.vadSample(0.2,2000); api.vadSample(0.2,2400);
  assert.equal(api.playbackActive(),'yes','acoustic overlap cannot barge in semantic mode');
  assert.equal(JSON.parse(api.pollEvents()).some(e=>e.type==='barge'||e.type==='speech-end'),false);
  api.vadSample(0,2500);
  assert.equal(JSON.parse(api.windowSnapshot()).user_speaking,false,'opening is retained without end-silence delay');
  assert.equal(api.windowSnapshot(),'','unchanged quiet audio does not repeatedly invoke STT');
  await api.dispose();
});

test('window capture bounds PCM, isolates utterances, and releases its graph',async()=>{
  const {api,seen}=makeFakes({windowCapture:true});
  api.windowConfigure(true);await api.startArmed();
  for(let n=0;n<35;n++)seen.processor.onaudioprocess({inputBuffer:{getChannelData:()=>new Float32Array(1000)}});
  api.vadSample(0.2,1000);
  const snapshot=JSON.parse(api.windowSnapshot());
  assert.equal(snapshot.start_ms,5000);assert.equal(snapshot.end_ms,35000);
  assert.equal(Buffer.from(snapshot.base64,'base64').length,44+30000*2);
  api.nextWindowUtterance();assert.equal(api.windowSnapshot(),'');
  await api.stopArmed();
  assert.equal(seen.captureDisconnected,true);assert.equal(seen.stoppedTracks,2);
  assert.equal(seen.processor.onaudioprocess,null);
});

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

function pcmLevels(segments, rate = 1000) {
  const samples = [];
  for (const [milliseconds, level] of segments) {
    for (let i = 0; i < Math.round(milliseconds * rate / 1000); i++) {
      samples.push(Math.round(level * 32767));
    }
  }
  return Buffer.from(new Int16Array(samples).buffer).toString('base64');
}

function mouthAt(api, seen, time) {
  seen.context.currentTime = time;
  return JSON.parse(api.mouthFrame());
}

test('mouth follows played PCM volume, closes in silence, and never opens the mic', async () => {
  const {api, seen} = makeFakes();
  assert.deepEqual(JSON.parse(api.mouthFrame()), {openness: 0, viseme: null, active: false});
  await api.queuePcm(pcmLevels([[60, 0], [60, 0.035], [60, 0.25], [20, 0]]), 1000);
  assert.deepEqual(mouthAt(api, seen, 100.01), {openness: 0, viseme: null, active: true});
  const moderate = mouthAt(api, seen, 100.07);
  assert.ok(moderate.openness > 0.3 && moderate.openness < 0.5);
  assert.ok(mouthAt(api, seen, 100.11).openness < 0.51);
  const loud = mouthAt(api, seen, 100.15);
  assert.ok(loud.openness > 0.8 && loud.openness <= 1);
  assert.equal(mouthAt(api, seen, 100.19).openness, 0, 'silence closes promptly between words');
  assert.equal(seen.constraints, undefined, 'sampling playback does not request microphone access');
  api.dispose();
});

test('queued future chunks and suspended context cannot animate ahead of played audio', async () => {
  const {api, seen} = makeFakes();
  await api.queuePcm(pcmLevels([[200, 0]]), 1000);
  await api.queuePcmWithVisemes(pcmLevels([[200, 0.25]]), 1000,
    [{start_ms: 0, end_ms: 200, viseme: 'aa'}]);
  assert.equal(mouthAt(api, seen, 100.1).openness, 0);
  assert.equal(mouthAt(api, seen, 100.21).viseme, 'aa');
  seen.context.state = 'suspended';
  assert.deepEqual(JSON.parse(api.mouthFrame()), {openness: 0, viseme: null, active: false});
  seen.context.state = 'running';
  assert.equal(mouthAt(api, seen, 100.25).viseme, 'aa');
  api.dispose();
});

test('mouth smoothing uses playback time and hysteresis rather than poll frequency', async () => {
  const {api, seen} = makeFakes();
  await api.queuePcm(pcmLevels([[40, 0.25], [40, 0.065], [40, 0.04], [40, 0.009]]), 1000);
  const first = mouthAt(api, seen, 100.01);
  for (let i = 0; i < 10; i++) assert.deepEqual(mouthAt(api, seen, 100.01), first);
  assert.ok(mouthAt(api, seen, 100.05).openness > first.openness,
    'open pose persists while above its lower release threshold');
  assert.ok(mouthAt(api, seen, 100.1).openness < 0.8,
    'moderate audio moves toward half open');
  assert.ok(mouthAt(api, seen, 100.15).openness > 0.4,
    'small noise changes around a boundary do not repeatedly close an open mouth');
  api.dispose();
});

test('valid visemes override volume, uncovered times use volume, silence stays closed', async () => {
  const {api, seen} = makeFakes();
  const audio = pcmLevels([[160, 0.25], [40, 0]]);
  assert.equal(await api.queuePcmWithVisemes(audio, 1000, JSON.stringify([
    {start_ms: 0, end_ms: 40, viseme: 'PP'},
    {start_ms: 40, end_ms: 80, viseme: 'aa'},
    {start_ms: 160, end_ms: 200, viseme: 'aa'},
  ])), 'queued');
  assert.deepEqual(mouthAt(api, seen, 100.01), {openness: 0, viseme: 'PP', active: true});
  assert.equal(mouthAt(api, seen, 100.05).viseme, 'aa');
  const uncovered = mouthAt(api, seen, 100.1);
  assert.equal(uncovered.viseme, null);
  assert.ok(uncovered.openness > 0.8);
  assert.equal(mouthAt(api, seen, 100.18).openness, 0);
  api.dispose();
});

test('malformed, overlapping, oversized and unsupported viseme lists fall back to PCM', async () => {
  const badLists = [
    'not JSON', {}, ' '.repeat(65537),
    [{start_ms: -1, end_ms: 20, viseme: 'PP'}],
    [{start_ms: '0', end_ms: 20, viseme: 'PP'}],
    [{start_ms: 0, end_ms: 0, viseme: 'PP'}],
    [{start_ms: 0, end_ms: NaN, viseme: 'PP'}],
    [{start_ms: 0, end_ms: 2000, viseme: 'PP'}],
    [{start_ms: 0, end_ms: 20, viseme: 'invented'}],
    [{start_ms: 0, end_ms: 20, viseme: 'toString'}],
    [{start_ms: 0, end_ms: 20, viseme: 'PP'}, {start_ms: 10, end_ms: 30, viseme: 'aa'}],
    Array.from({length: 257}, (_, i) => ({start_ms: i, end_ms: i + 1, viseme: 'PP'})),
  ];
  for (const cues of badLists) {
    const {api, seen} = makeFakes();
    assert.equal(await api.queuePcmWithVisemes(pcmLevels([[1000, 0.25]]), 1000, cues), 'queued');
    const frame = mouthAt(api, seen, 100.01);
    assert.equal(frame.viseme, null);
    assert.ok(frame.openness > 0.7, 'malformed cues never mute valid PCM amplitude');
    api.dispose();
  }
});

test('natural end closes the mouth before onended but never acknowledges playback early', async () => {
  const {api, seen, sources} = makeFakes();
  await api.queuePcm(pcmLevels([[100, 0.25]]), 1000);
  assert.ok(mouthAt(api, seen, 100.01).openness > 0);
  let ended = false;
  const waiter = api.awaitPlayback().then(() => { ended = true; });
  assert.deepEqual(mouthAt(api, seen, 100.11), {openness: 0, viseme: null, active: false});
  await Promise.resolve();
  assert.equal(ended, false);
  sources[0].onended();
  await waiter;
  assert.equal(ended, true);
  assert.equal(JSON.parse(api.mouthFrame()).active, false);
  api.dispose();
});

test('stop, disposal and stale pending PCM clear mouth state across generations', async () => {
  const {api, seen, sources} = makeFakes();
  const audio = pcmLevels([[1000, 0.25]]);
  await api.queuePcmWithVisemes(audio, 1000, [{start_ms: 0, end_ms: 1000, viseme: 'aa'}]);
  assert.equal(mouthAt(api, seen, 100.01).viseme, 'aa');
  api.stopAudio();
  assert.deepEqual(JSON.parse(api.mouthFrame()), {openness: 0, viseme: null, active: false});
  await api.queuePcm(audio, 1000);
  sources[0].onended();
  assert.equal(mouthAt(api, seen, 100.02).active, true, 'stale callback cannot clear new speech');
  api.dispose();
  assert.deepEqual(JSON.parse(api.mouthFrame()), {openness: 0, viseme: null, active: false});

  const pending = makeFakes({suspended: true, holdResume: true});
  const result = pending.api.queuePcmWithVisemes(audio, 1000,
    [{start_ms: 0, end_ms: 1000, viseme: 'aa'}]);
  pending.api.stopAudio();
  pending.resumeGate.release();
  assert.equal(await result, 'stopped');
  assert.equal(JSON.parse(pending.api.mouthFrame()).active, false);
  pending.api.dispose();
});

test('mouth metadata has bounded chunks/windows and expires after playback', async () => {
  const {api, seen, sources} = makeFakes({trackEnvelopes: true});
  const audio = pcmLevels([[30000, 0.25]]);
  for (let i = 0; i < 140; i++) await api.queuePcm(audio, 1000);
  assert.equal(seen.envelopeSizes.length, 128);
  assert.ok(seen.envelopeSizes.every(size => size <= 1000));
  for (const source of sources) source.onended();
  assert.equal(JSON.parse(api.mouthFrame()).active, false);
  await api.queuePcm(audio, 1000);
  assert.equal(seen.envelopeSizes.length, 129, 'natural completion releases the metadata budget');
  api.dispose();
});

// Keep the existing CI entrypoint responsible for both trusted browser bridges.
require('./presentation_bridge.cases.js');
