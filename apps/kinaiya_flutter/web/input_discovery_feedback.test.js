// Requirement-first discovery feedback; synthetic browser metadata only.
'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

test('one redacted input identifier refuses the entire inventory without opening a microphone', async () => {
  const seen = {enumerations: 0, microphones: 0, contexts: 0};
  class Context {
    constructor() { seen.contexts++; this.state = 'running'; this.sinkId = ''; }
    setSinkId(id) { this.sinkId = id; return Promise.resolve(); }
    resume() { return Promise.resolve(); }
    close() { this.state = 'closed'; return Promise.resolve(); }
  }
  const mediaDevices = {
    async enumerateDevices() {
      seen.enumerations++;
      return [
        {kind: 'audioinput', deviceId: 'synthetic-valid-input', label: 'Synthetic input'},
        {kind: 'audioinput', deviceId: '', label: 'Browser-redacted input'},
      ];
    },
    getUserMedia() {
      seen.microphones++;
      return Promise.reject(new Error('Discovery must not request capture'));
    },
    addEventListener() {},
    removeEventListener() {},
  };
  class Recorder { static isTypeSupported() { return true; } }
  const sandbox = {
    console, Promise, JSON, Object, Array, Math, Number, Uint8Array, Int16Array,
    Float32Array, ArrayBuffer, DataView, Blob, Date, queueMicrotask,
    setTimeout, clearTimeout, setInterval: () => 1, clearInterval() {},
    atob: value => Buffer.from(value, 'base64').toString('binary'),
    btoa: value => Buffer.from(value, 'binary').toString('base64'),
    navigator: {mediaDevices}, MediaRecorder: Recorder,
  };
  sandbox.window = {
    AudioContext: Context, navigator: sandbox.navigator, Blob,
    atob: sandbox.atob, btoa: sandbox.btoa,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, 'audio_bridge.js'), 'utf8'),
    sandbox, {filename: 'audio_bridge.js'},
  );
  const audio = sandbox.window.kinaiyaAudio;
  try {
    const wire = await audio.inputDevices();
    assert.equal(typeof wire, 'string');
    const result = JSON.parse(wire);
    assert.ok(seen.enumerations > 0, 'the actual browser inventory was consulted');
    assert.equal(result.supported, true, 'capability exists; identifiers are unavailable');
    assert.equal(result.status, 'unavailable');
    assert.equal(result.reason, 'identifiers-unavailable');
    assert.deepEqual(result.devices, [], 'the valid prefix cannot become a partial inventory');
    assert.equal(result.pending, false);
    assert.equal(result.explicit, false, 'discovery cannot select an implicit fallback');
    assert.equal(seen.microphones, 0);
    assert.equal(seen.contexts, 0);
  } finally {
    await audio.dispose();
  }
  assert.equal(seen.microphones, 0);
});
