/// Independent import/playback contract checks. No real media or network.
/// Run with Node's test runner; this module does not import another test suite.
'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function bridge(options = {}) {
  const seen = {
    order: [], contexts: [], sources: [], inputs: [], resumes: [],
    sourceCalls: 0, microphoneCalls: 0, gesture: false,
  };
  const pendingResumes = [];

  class Input {
    constructor() { this.handlers = new Map(); this.files = []; }
    addEventListener(name, callback) { this.handlers.set(name, callback); }
    click() {
      seen.order.push('picker.click');
      // Requiring resume before click also covers a picker consuming activation.
      seen.gesture = false;
    }
    fire(name) {
      const handler = this.handlers.get(name);
      assert.equal(typeof handler, 'function', `picker ${name} handler exists`);
      return handler();
    }
  }

  class Source {
    constructor(context) { this.context = context; this.onended = null; }
    connect() {}
    disconnect() {}
    start(at) { this.startedAt = at; }
    stop() { this.stopped = true; }
    end() {
      assert.equal(typeof this.onended, 'function');
      this.onended();
    }
  }

  class Context {
    constructor() {
      this.state = 'suspended';
      this.currentTime = 10;
      this.destination = {};
      seen.contexts.push(this);
      seen.order.push('context.create');
    }
    resume() {
      seen.order.push('context.resume');
      seen.resumes.push({ context: this, gesture: seen.gesture });
      if (options.resume === 'held') {
        return new Promise(resolve => pendingResumes.push(() => {
          this.state = 'running';
          resolve();
        }));
      }
      if (options.resume === 'still-suspended') return Promise.resolve();
      if (!seen.gesture && options.gestureOnly) {
        return Promise.reject(new Error('Playback requires an actual gesture'));
      }
      this.state = 'running';
      return Promise.resolve();
    }
    createBuffer(channels, count, rate) {
      assert.equal(channels, 1);
      const samples = new Float32Array(count);
      return { duration: count / rate, getChannelData: () => samples };
    }
    createBufferSource() {
      seen.sourceCalls++;
      assert.equal(this.state, 'running', 'a source needs a running context');
      const source = new Source(this);
      seen.sources.push(source);
      return source;
    }
    close() { this.state = 'closed'; return Promise.resolve(); }
  }

  const navigator = {
    mediaDevices: {
      getUserMedia() {
        seen.microphoneCalls++;
        throw new Error('An imported recording must not acquire a microphone');
      },
    },
  };
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
    document: {
      createElement(tag) {
        assert.equal(tag, 'input');
        const input = new Input();
        seen.inputs.push(input);
        return input;
      },
    },
  };
  sandbox.window = {
    AudioContext: options.noAudio ? undefined : Context,
    navigator, Blob, atob: sandbox.atob, btoa: sandbox.btoa,
    document: sandbox.document,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, 'audio_bridge.js'), 'utf8'),
    sandbox,
    { filename: 'audio_bridge.js' },
  );
  const api = sandbox.window.kinaiyaAudio;
  return {
    api, seen,
    choose() {
      seen.gesture = true;
      try { return api.chooseRecording(); }
      finally { seen.gesture = false; }
    },
    releaseResumes() {
      while (pendingResumes.length) pendingResumes.shift()();
    },
    clean() {
      api.dispose();
      while (pendingResumes.length) pendingResumes.shift()();
    },
  };
}

function recording(bytes = [82, 73, 70, 70]) {
  const raw = Uint8Array.from(bytes);
  return {
    name: 'owner-recording.wav', type: 'audio/wav', size: raw.length,
    arrayBuffer: async () => raw.buffer,
  };
}

const pcm = () => Buffer.alloc(480, 1).toString('base64');
const flush = async () => {
  for (let i = 0; i < 4; i++) await Promise.resolve();
};
const noCaptureOrSource = seen => {
  assert.equal(seen.microphoneCalls, 0);
  assert.equal(seen.sourceCalls, 0);
  assert.equal(seen.sources.length, 0);
};

test('import requests resume synchronously before the picker without awaiting it', async t => {
  const host = bridge({ resume: 'held' });
  t.after(() => host.clean());
  const chosen = host.choose();
  assert.equal(host.seen.resumes.length, 1);
  assert.equal(host.seen.resumes[0].gesture, true);
  assert.ok(host.seen.order.indexOf('context.resume') <
    host.seen.order.indexOf('picker.click'));
  assert.equal(host.seen.inputs.length, 1, 'held resume never blocks the picker');
  assert.equal(host.seen.contexts[0].state, 'suspended');
  host.seen.inputs[0].fire('cancel');
  assert.equal(await chosen, '', 'cancelling does not wait for resume either');
  noCaptureOrSource(host.seen);
});

test('later PCM uses the same gesture-prepared context and waits for real onended', async t => {
  const host = bridge({ gestureOnly: true });
  t.after(() => host.clean());
  const chosen = host.choose();
  host.seen.inputs[0].files = [recording()];
  await host.seen.inputs[0].fire('change');
  const selected = JSON.parse(await chosen);
  assert.equal(selected.filename, 'owner-recording.wav');
  assert.equal(selected.mime, 'audio/wav');
  noCaptureOrSource(host.seen);
  await flush(); // File intake/network/generation need no retained fake gesture.
  assert.equal(host.seen.gesture, false);
  assert.equal(await host.api.queuePcm(pcm(), 24000), 'queued');
  assert.equal(host.seen.contexts.length, 1);
  assert.equal(host.seen.resumes.length, 1, 'late queue needs no second activation');
  assert.equal(host.seen.sources[0].context, host.seen.contexts[0]);
  assert.equal(host.seen.microphoneCalls, 0);
  let settled = false;
  const played = host.api.awaitPlayback().then(value => { settled = true; return value; });
  await flush();
  assert.equal(settled, false, 'queued/synthesis completion is not playback');
  host.seen.sources[0].end();
  assert.equal(await played, 'done');
});

for (const selection of ['cancel', 'missing-file', 'oversized']) {
  test(`import ${selection} acquires no microphone and queues no PCM`, async t => {
    const host = bridge();
    t.after(() => host.clean());
    const chosen = host.choose();
    const input = host.seen.inputs[0];
    if (selection === 'cancel') input.fire('cancel');
    else {
      input.files = selection === 'missing-file' ? [] : [{ size: 10000001 }];
      await input.fire('change');
    }
    assert.equal(await chosen, '');
    noCaptureOrSource(host.seen);
    assert.equal(host.api.playbackActive(), 'no');
  });
}

test('unsupported playback does not block cancelling a picker or acquire a mic', async t => {
  const host = bridge({ noAudio: true });
  t.after(() => host.clean());
  const chosen = host.choose();
  assert.equal(host.seen.inputs.length, 1);
  host.seen.inputs[0].fire('cancel');
  assert.equal(await chosen, '');
  await flush();
  noCaptureOrSource(host.seen);
});

test('malformed PCM after import preparation is never queued', async t => {
  const host = bridge();
  t.after(() => host.clean());
  const chosen = host.choose();
  host.seen.inputs[0].fire('cancel');
  await chosen;
  const result = await host.api.queuePcm('not!base64', 24000).catch(() => 'refused');
  assert.notEqual(result, 'queued');
  noCaptureOrSource(host.seen);
});

test('a fulfilled resume that leaves context suspended is not queued playback', async t => {
  const host = bridge({ resume: 'still-suspended' });
  t.after(() => host.clean());
  const chosen = host.choose();
  host.seen.inputs[0].fire('cancel');
  await chosen;
  const result = await host.api.queuePcm(pcm(), 24000).catch(() => 'refused');
  assert.notEqual(result, 'queued');
  assert.equal(host.seen.contexts[0].state, 'suspended');
  noCaptureOrSource(host.seen);
  assert.equal(host.api.playbackActive(), 'no');
});

test('stop during held resume prevents late old PCM from creating a source', async t => {
  const host = bridge({ resume: 'held' });
  t.after(() => host.clean());
  const chosen = host.choose();
  host.seen.inputs[0].fire('cancel');
  await chosen;
  const queued = host.api.queuePcm(pcm(), 24000);
  assert.equal(host.api.stopAudio(), 'stopped');
  host.releaseResumes();
  assert.equal(await queued, 'stopped');
  noCaptureOrSource(host.seen);
  assert.equal(host.api.playbackActive(), 'no');
});

test('an old ended callback cannot settle a newer import-prepared playback', async t => {
  const host = bridge({ gestureOnly: true });
  t.after(() => host.clean());
  const chosen = host.choose();
  host.seen.inputs[0].fire('cancel');
  await chosen;
  assert.equal(await host.api.queuePcm(pcm(), 24000), 'queued');
  const old = host.seen.sources[0];
  host.api.stopAudio();
  assert.equal(await host.api.queuePcm(pcm(), 24000), 'queued');
  let settled = false;
  const played = host.api.awaitPlayback().then(value => { settled = true; return value; });
  old.end();
  await flush();
  assert.equal(settled, false);
  host.seen.sources[1].end();
  assert.equal(await played, 'done');
  assert.equal(host.seen.microphoneCalls, 0);
});
