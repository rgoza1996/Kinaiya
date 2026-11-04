// Independent optional-output contract oracles. No real devices or network.
// Authored from the public handshake without reading the implementation diff.
'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const MARKER = 'UNTRUSTED-SYNTHETIC-OUTPUT-ERROR';
const PCM = Buffer.from(new Int16Array([1, -2, 300, -400]).buffer).toString('base64');
const output = (id, label = id) => ({ kind: 'audiooutput', deviceId: id, label });
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

async function json(promise) {
  assert.ok(promise && typeof promise.then === 'function', 'output bridge methods return Promises');
  const raw = await promise;
  assert.equal(typeof raw, 'string');
  assert.ok(raw.length <= 2 * 1024 * 1024, 'bounded local device metadata');
  assert.equal(raw.includes(MARKER), false, 'raw browser errors are not reflected');
  return JSON.parse(raw);
}

function host(options = {}) {
  const seen = {
    mic: 0, enumerations: 0, pickers: [], contexts: [], sinks: [],
    sources: [], order: [], resumes: [], activeSinks: new Set(),
  };
  let devices = options.devices || [output('speaker-a', 'Public speakers A'), output('speaker-b', 'Public speakers B')];
  const enumerationGates = [], pickerGates = [], resumeGates = [];
  const sinkPlans = [...(options.sinkPlans || [])];
  const listeners = new Map();
  const mediaDevices = {
    getUserMedia() {
      seen.mic++;
      throw new Error('Output selection must never acquire a microphone');
    },
    enumerateDevices() {
      seen.enumerations++;
      if (options.enumerationError) return Promise.reject(new Error(MARKER));
      if (options.holdEnumeration) {
        const gate = deferred();
        enumerationGates.push(gate);
        return gate.promise;
      }
      return Promise.resolve(devices.slice());
    },
    selectAudioOutput() {
      seen.pickers.push({ gesture: seen.gesture });
      const gate = deferred();
      pickerGates.push(gate);
      return gate.promise;
    },
    addEventListener(name, listener) {
      if (!listeners.has(name)) listeners.set(name, new Set());
      listeners.get(name).add(listener);
    },
    removeEventListener(name, listener) { listeners.get(name)?.delete(listener); },
  };
  if (options.noEnumeration) delete mediaDevices.enumerateDevices;
  if (options.noPicker) delete mediaDevices.selectAudioOutput;

  class Source {
    constructor(context) { this.context = context; this.onended = null; }
    connect() {}
    disconnect() { this.disconnected = true; }
    start(at) {
      assert.equal(seen.activeSinks.size, 0, 'no PCM source starts before actual routing settles');
      assert.equal(this.context.state, 'running');
      this.startedAt = at;
      this.sinkAtStart = this.context.sinkId;
      seen.order.push('source.start:' + this.sinkAtStart);
    }
    stop() {
      this.stopped = true;
      queueMicrotask(() => { if (this.onended) this.onended(); });
    }
    end() {
      assert.equal(typeof this.onended, 'function');
      this.onended();
    }
  }

  class Context {
    constructor() {
      this.currentTime = 5;
      this.state = 'running';
      this.sinkId = '';
      this.destination = {};
      seen.contexts.push(this);
      seen.order.push('context.create');
    }
    resume() {
      seen.resumes.push(this);
      if (options.holdResume && this.state === 'suspended') {
        const gate = deferred();
        resumeGates.push({ context: this, gate });
        return gate.promise.then(() => { this.state = 'running'; });
      }
      this.state = 'running';
      return Promise.resolve();
    }
    setSinkId(id) {
      assert.equal(typeof id, 'string');
      const gate = deferred();
      const plan = sinkPlans.shift() || 'success';
      const record = { context: this, id, gate, observed: id };
      seen.sinks.push(record);
      seen.activeSinks.add(record);
      seen.order.push('sink.begin:' + id);
      if (plan === 'success') gate.resolve();
      else if (plan === 'failure') gate.reject(new Error(MARKER));
      else if (plan === 'mismatch') { record.observed = 'foreign-output'; gate.resolve(); }
      else assert.equal(plan, 'held');
      return gate.promise.then(() => {
        this.sinkId = record.observed;
        seen.order.push('sink.end:' + id);
      }).finally(() => { seen.activeSinks.delete(record); });
    }
    createBuffer(channels, samples, rate) {
      assert.equal(channels, 1);
      const values = new Float32Array(samples);
      return { duration: samples / rate, getChannelData: () => values };
    }
    createBufferSource() {
      const source = new Source(this);
      seen.sources.push(source);
      return source;
    }
    close() { this.state = 'closed'; return Promise.resolve(); }
  }
  if (options.noSink) delete Context.prototype.setSinkId;
  const navigator = { mediaDevices };
  const sandbox = {
    console, Promise, JSON, Math, Date, Array, Object, Blob,
    Uint8Array, Float32Array, ArrayBuffer, DataView,
    setTimeout, clearTimeout, setInterval, clearInterval, queueMicrotask,
    navigator,
    atob: value => {
      const bytes = Buffer.from(value, 'base64');
      if (bytes.toString('base64') !== value) throw new Error('Invalid base64');
      return bytes.toString('binary');
    },
    btoa: value => Buffer.from(value, 'binary').toString('base64'),
  };
  sandbox.window = {
    AudioContext: options.noAudio ? undefined : Context,
    navigator, Blob, atob: sandbox.atob, btoa: sandbox.btoa,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, 'audio_bridge.js'), 'utf8'), sandbox,
    { filename: 'audio_bridge.js' });
  const api = sandbox.window.kinaiyaAudio;
  return {
    api, seen,
    devices(value) { devices = value; },
    picker() {
      seen.gesture = true;
      try { return api.chooseOutput(); }
      finally { seen.gesture = false; }
    },
    resolvePicker(value, index = pickerGates.length - 1) { pickerGates[index].resolve(value); },
    rejectPicker(index = pickerGates.length - 1) {
      pickerGates[index].reject(Object.assign(new Error(MARKER), { name: 'NotAllowedError' }));
    },
    resolveSink(index = seen.sinks.length - 1) { seen.sinks[index].gate.resolve(); },
    resolveEnumeration(value, index = enumerationGates.length - 1) {
      enumerationGates[index].resolve(value);
    },
    releaseEnumerations(value) {
      options.holdEnumeration = false;
      for (const gate of enumerationGates) gate.resolve(value);
    },
    resolveResume() {
      const entry = resumeGates.shift();
      assert.ok(entry, 'an actual original resume is held');
      entry.gate.resolve();
    },
    deviceChanged() {
      for (const callback of listeners.get('devicechange') || []) callback();
      if (typeof mediaDevices.ondevicechange === 'function') mediaDevices.ondevicechange();
    },
    clean() {
      api.dispose();
      for (const record of seen.sinks) record.gate.resolve();
      for (const gate of enumerationGates) gate.resolve([]);
      for (const gate of pickerGates) gate.resolve(output('speaker-a'));
      for (const entry of resumeGates) entry.gate.resolve();
    },
  };
}

test('device discovery returns bounded output-only metadata and never opens a microphone', async t => {
  const h = host({ devices: [output('speaker-a', 'A'), { kind: 'audioinput', deviceId: 'private-mic', label: 'Microphone' }] });
  t.after(() => h.clean());
  const info = await json(h.api.outputDevices());
  assert.equal(info.supported, true);
  assert.equal(info.picker, true);
  assert.equal(info.pending, false);
  assert.equal(info.explicit, false);
  assert.equal(info.failed, false);
  assert.deepEqual(info.devices, [{ id: 'speaker-a', label: 'A', selected: false }]);
  assert.equal(h.seen.mic, 0);
  assert.equal(h.seen.sinks.length, 0);
  assert.equal(h.seen.sources.length, 0);
});

test('the exact 128-device and 4096-character ID/label bounds remain usable', async t => {
  const longId = 'x'.repeat(4096), longLabel = 'y'.repeat(4096);
  const devices = [output(longId, longLabel), ...Array.from({ length: 127 }, (_, n) => output('public-' + n))];
  const h = host({ devices });
  t.after(() => h.clean());
  const info = await json(h.api.outputDevices());
  assert.equal(info.devices.length, 128);
  assert.deepEqual(info.devices[0], { id: longId, label: longLabel, selected: false });
  assert.equal((await json(h.api.selectOutput(longId))).status, 'selected');
  assert.equal(h.seen.sinks[0].id, longId);
  assert.equal(h.seen.mic, 0);
});

for (const invalid of [
  Array.from({ length: 129 }, (_, n) => output('public-' + n)),
  [output('')], [output('x'.repeat(4097))],
  [output('speaker-a', 'y'.repeat(4097))],
  [{ kind: 'audiooutput', deviceId: 42, label: 'A' }],
  [{ kind: 'audiooutput', deviceId: 'speaker-a', label: 42 }],
]) {
  test('malformed or oversized discovery is all-or-refuse without poisoning implicit default', async t => {
    const h = host({ devices: invalid });
    t.after(() => h.clean());
    const info = await json(h.api.outputDevices());
    assert.equal(info.status, 'unavailable');
    assert.deepEqual(info.devices, []);
    assert.equal(h.seen.mic, 0);
    assert.equal(h.seen.sinks.length, 0);
    assert.equal(await h.api.queuePcm(PCM, 24000), 'queued');
    assert.equal(h.seen.sources[0].sinkAtStart, '');
  });
}

for (const options of [{ noAudio: true }, { noSink: true }, { noEnumeration: true }]) {
  test('missing output capabilities remain explicit and never request microphone permission', async t => {
    const h = host(options);
    t.after(() => h.clean());
    const info = await json(h.api.outputDevices());
    assert.equal(info.supported, false);
    assert.deepEqual(info.devices, []);
    assert.equal(h.seen.mic, 0);
    assert.equal(h.seen.sinks.length, 0);
    assert.equal(h.seen.sources.length, 0);
  });
}

test('selected routing settles on the exact context before PCM and onended owns delivery', async t => {
  const h = host();
  t.after(() => h.clean());
  assert.equal((await json(h.api.selectOutput('speaker-a'))).status, 'selected');
  const routing = h.seen.sinks[0];
  assert.equal(routing.id, 'speaker-a');
  assert.equal(routing.context.sinkId, 'speaker-a');
  assert.equal(await h.api.queuePcm(PCM, 24000), 'queued');
  assert.equal(h.seen.sources[0].context, routing.context);
  assert.equal(h.seen.sources[0].sinkAtStart, 'speaker-a');
  assert.ok(h.seen.order.indexOf('sink.end:speaker-a') < h.seen.order.indexOf('source.start:speaker-a'));
  let outcome = null;
  const original = h.api.awaitPlayback().then(value => { outcome = value; return value; });
  await flush();
  assert.equal(outcome, null, 'routing success and queued PCM are not playback completion');
  h.seen.sources[0].end();
  assert.equal(await original, 'done');
  assert.equal(h.api.playbackActive(), 'no');
  assert.equal(h.seen.mic, 0);
});

test('one original held setSinkId blocks PCM and all competing selections', async t => {
  const h = host({ sinkPlans: ['held'] });
  t.after(() => h.clean());
  const original = h.api.selectOutput('speaker-a');
  await flush();
  assert.equal(h.seen.sinks.length, 1);
  assert.equal((await json(h.api.outputDevices())).pending, true);
  assert.equal(await h.api.queuePcm(PCM, 24000), 'stopped');
  assert.equal(await h.api.queuePcmWithVisemes(PCM, 24000, '[]'), 'stopped');
  assert.equal((await json(h.api.selectOutput('speaker-b'))).status, 'busy');
  assert.equal((await json(h.api.defaultOutput())).status, 'busy');
  assert.equal((await json(h.picker())).status, 'busy');
  assert.equal(h.seen.pickers.length, 0);
  assert.equal(h.seen.sinks.length, 1);
  assert.equal(h.seen.sources.length, 0);
  h.resolveSink();
  assert.equal((await json(original)).status, 'selected');
  assert.equal((await json(h.api.outputDevices())).pending, false);
  assert.equal(await h.api.queuePcm(PCM, 24000), 'queued');
});

test('selection owns its slot before the first held device-discovery await', async t => {
  const h = host({ holdEnumeration: true });
  t.after(() => h.clean());
  const original = h.api.selectOutput('speaker-a');
  await flush();
  assert.ok(h.seen.enumerations > 0);
  assert.equal(await h.api.queuePcm(PCM, 24000), 'stopped');
  assert.equal((await json(h.api.selectOutput('speaker-b'))).status, 'busy');
  assert.equal((await json(h.api.defaultOutput())).status, 'busy');
  assert.equal((await json(h.picker())).status, 'busy');
  assert.equal(h.seen.sinks.length, 0);
  assert.equal(h.seen.sources.length, 0);
  h.releaseEnumerations([output('speaker-a'), output('speaker-b')]);
  assert.equal((await json(original)).status, 'selected');
  assert.equal(h.seen.sinks.length, 1);
  assert.equal(h.seen.sinks[0].id, 'speaker-a');
});

test('the real browser output picker is invoked synchronously and its original result is owned', async t => {
  const h = host();
  t.after(() => h.clean());
  const original = h.picker();
  assert.equal(h.seen.pickers.length, 1, 'no await may consume activation before picker call');
  assert.equal(h.seen.pickers[0].gesture, true);
  await flush();
  assert.equal((await json(h.api.outputDevices())).pending, true);
  assert.equal(await h.api.queuePcm(PCM, 24000), 'stopped');
  assert.equal((await json(h.api.selectOutput('speaker-b'))).status, 'busy');
  h.resolvePicker(output('speaker-b', 'Public speakers B'));
  assert.equal((await json(original)).status, 'selected');
  assert.equal(h.seen.sinks.length, 1);
  assert.equal(h.seen.sinks[0].id, 'speaker-b');
  assert.equal(await h.api.queuePcm(PCM, 24000), 'queued');
  assert.equal(h.seen.sources[0].sinkAtStart, 'speaker-b');
  assert.equal(h.seen.mic, 0);
});

test('cancelled picker before routing leaves the previous explicit output intact', async t => {
  const h = host();
  t.after(() => h.clean());
  await json(h.api.selectOutput('speaker-a'));
  const original = h.picker();
  h.rejectPicker();
  assert.equal((await json(original)).status, 'cancelled');
  assert.equal(h.seen.sinks.length, 1, 'cancel did not dispatch another routing operation');
  assert.equal(await h.api.queuePcm(PCM, 24000), 'queued');
  assert.equal(h.seen.sources[0].sinkAtStart, 'speaker-a');
});

test('active scheduled sources refuse output changes without stopping original playback', async t => {
  const h = host();
  t.after(() => h.clean());
  assert.equal(await h.api.queuePcm(PCM, 24000), 'queued');
  const source = h.seen.sources[0];
  assert.equal((await json(h.api.selectOutput('speaker-a'))).status, 'busy');
  assert.equal((await json(h.api.defaultOutput())).status, 'busy');
  assert.equal((await json(h.picker())).status, 'busy');
  assert.equal(h.seen.sinks.length, 0);
  assert.equal(h.seen.pickers.length, 0);
  assert.equal(source.stopped, undefined);
  const original = h.api.awaitPlayback();
  source.end();
  assert.equal(await original, 'done');
  assert.equal((await json(h.api.selectOutput('speaker-a'))).status, 'selected');
});

test('disposal cannot let an old held routing completion bind a replacement context', async t => {
  const h = host({ sinkPlans: ['held'] });
  t.after(() => h.clean());
  const original = h.api.selectOutput('speaker-a');
  await flush();
  const old = h.seen.sinks[0].context;
  assert.equal(h.api.dispose(), 'disposed');
  assert.equal(old.state, 'closed');
  assert.equal(await h.api.queuePcm(PCM, 24000), 'stopped');
  assert.equal((await json(h.api.selectOutput('speaker-b'))).status, 'busy');
  assert.equal(h.seen.sources.length, 0);
  h.resolveSink();
  assert.equal((await json(original)).status, 'stopped');
  assert.equal(h.seen.sources.length, 0);
  assert.equal((await json(h.api.defaultOutput())).status, 'default');
  assert.equal((await json(h.api.selectOutput('speaker-b'))).status, 'selected');
  assert.equal(await h.api.queuePcm(PCM, 24000), 'queued');
  const replacement = h.seen.sources[0].context;
  assert.notEqual(replacement, old);
  assert.equal(h.seen.sources[0].sinkAtStart, 'speaker-b');
  assert.equal(h.seen.sinks.filter(record => record.context === replacement && record.id === 'speaker-a').length, 0);
});

for (const plan of ['failure', 'mismatch']) {
  test('explicit routing ' + plan + ' forbids silent default until an owner default succeeds', async t => {
    const h = host({ sinkPlans: [plan] });
    t.after(() => h.clean());
    assert.equal((await json(h.api.selectOutput('speaker-a'))).status, 'failed');
    const info = await json(h.api.outputDevices());
    assert.equal(info.explicit, true);
    assert.equal(info.failed, true);
    assert.equal(await h.api.queuePcm(PCM, 24000), 'stopped');
    assert.equal(h.seen.sources.length, 0);
    assert.equal((await json(h.api.defaultOutput())).status, 'default');
    assert.equal(await h.api.queuePcm(PCM, 24000), 'queued');
    assert.equal(h.seen.sources[0].sinkAtStart, '');
    assert.equal(h.seen.mic, 0);
  });
}

test('a removed explicitly selected device never silently routes to system default', async t => {
  const h = host();
  t.after(() => h.clean());
  await json(h.api.selectOutput('speaker-a'));
  h.devices([output('speaker-b')]);
  const info = await json(h.api.outputDevices());
  assert.equal(info.explicit, true);
  assert.equal(info.failed, true);
  assert.equal(await h.api.queuePcm(PCM, 24000), 'stopped');
  assert.equal(h.seen.sources.length, 0);
  assert.equal(h.seen.sinks.length, 1);
});

test('devicechange stops explicit playback and requires an owner routing decision', async t => {
  const h = host();
  t.after(() => h.clean());
  await json(h.api.selectOutput('speaker-a'));
  await h.api.queuePcm(PCM, 24000);
  const original = h.api.awaitPlayback();
  h.deviceChanged();
  await flush();
  assert.equal(await original, 'stopped');
  assert.equal(h.seen.sources[0].stopped, true);
  assert.equal(await h.api.queuePcm(PCM, 24000), 'stopped');
  assert.equal(h.seen.sinks.length, 1);
  assert.equal((await json(h.api.defaultOutput())).status, 'default');
  assert.equal(await h.api.queuePcm(PCM, 24000), 'queued');
});

test('external sink mismatch is checked again after a held context resume', async t => {
  const h = host({ holdResume: true });
  t.after(() => h.clean());
  await json(h.api.selectOutput('speaker-a'));
  const context = h.seen.sinks[0].context;
  context.state = 'suspended';
  const original = h.api.queuePcm(PCM, 24000);
  await flush();
  assert.equal(h.seen.resumes.at(-1), context);
  context.sinkId = 'speaker-b';
  h.resolveResume();
  assert.equal(await original, 'stopped');
  assert.equal(h.seen.sources.length, 0);
  assert.equal((await json(h.api.outputDevices())).failed, true);
});

test('foreign or nonenumerated device identifiers cannot route or invoke conversion hooks', async t => {
  const h = host();
  t.after(() => h.clean());
  let converted = false;
  const foreign = { toString() { converted = true; throw new Error(MARKER); } };
  for (const id of ['', 'not-enumerated', 'x'.repeat(4097), null, true, foreign]) {
    assert.equal((await json(h.api.selectOutput(id))).status, 'invalid');
  }
  assert.equal(converted, false);
  assert.equal(h.seen.sinks.length, 0);
  assert.equal(h.seen.mic, 0);
  assert.equal(await h.api.queuePcm(PCM, 24000), 'queued');
  assert.equal(h.seen.sources[0].sinkAtStart, '');
});

test('legacy default playback requires no output-selection APIs and keeps real onended semantics', async t => {
  const h = host({ noSink: true, noEnumeration: true, noPicker: true });
  t.after(() => h.clean());
  assert.equal((await json(h.api.outputDevices())).supported, false);
  assert.equal((await json(h.picker())).status, 'unavailable');
  assert.equal(await h.api.queuePcm(PCM, 24000), 'queued');
  let settled = false;
  const original = h.api.awaitPlayback().then(value => { settled = true; return value; });
  await flush();
  assert.equal(settled, false);
  h.seen.sources[0].end();
  assert.equal(await original, 'done');
  assert.equal(h.seen.sinks.length, 0);
  assert.equal(h.seen.mic, 0);
});
