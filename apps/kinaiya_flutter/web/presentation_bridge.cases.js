'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, 'presentation_bridge.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness(options = {}) {
  const events = new Map();
  const deadlines = new Set();
  const elements = [];
  const opens = [];
  const transactions = [];
  const held = [];
  const revoked = [];
  const blobs = [];
  const state = {value: options.value, closes: 0, clicks: 0};
  const window = {
    addEventListener(name, callback) { events.set(name, callback); },
    removeEventListener(name) { events.delete(name); },
    indexedDB: {
      open(name, version) {
        opens.push([name, version]);
        if (options.openThrows) throw new Error('private error');
        const request = {};
        queueMicrotask(() => {
          if (options.openStalls) return;
          if (options.blocked) return request.onblocked();
          request.result = database;
          request.onupgradeneeded();
          request.onsuccess();
        });
        return request;
      },
    },
  };
  const database = {
    objectStoreNames: {contains: () => false},
    createObjectStore(name) { assert.equal(name, 'snapshot'); },
    close() { state.closes++; },
    transaction(name, mode) {
      assert.equal(name, 'snapshot');
      const tx = {
        aborted: false,
        abort() { this.aborted = true; this.onabort?.(); },
        objectStore(storeName) {
          assert.equal(storeName, 'snapshot');
          const request = {};
          const perform = (write, value, key) => {
            assert.equal(key, 'current');
            const complete = () => {
              if (tx.aborted) return;
              if (options.failTransaction) return tx.onerror();
              request.result = state.value;
              request.onsuccess?.();
              if (tx.aborted) return;
              if (write) state.value = value;
              tx.oncomplete();
            };
            if (write && options.holdWrites) held.push(complete);
            else queueMicrotask(complete);
            return request;
          };
          return {get: key => perform(false, undefined, key),
            put: (value, key) => perform(true, value, key)};
        },
      };
      transactions.push([name, mode]);
      return tx;
    },
  };
  const document = {
    body: {appendChild(element) { element.attached = true; }},
    createElement(tag) {
      const listeners = new Map();
      const element = {
        tag, style: {}, files: [], removed: false,
        addEventListener(name, callback) { listeners.set(name, callback); },
        async dispatch(name) { return listeners.get(name)?.(); },
        click() { state.clicks++; options.onClick?.(element); },
        remove() { this.removed = true; },
      };
      elements.push(element);
      return element;
    },
  };
  vm.runInNewContext(source, {
    window, document, TextEncoder,
    Blob: class { constructor(parts, options) { blobs.push({parts, options}); } },
    URL: {createObjectURL: () => 'blob:local-test', revokeObjectURL: value => revoked.push(value)},
    setTimeout(callback, delay) {
      if (delay === 5000) { deadlines.add(callback); return callback; }
      return setTimeout(callback, 0);
    },
    clearTimeout(timer) { if (typeof timer === 'function') deadlines.delete(timer); else clearTimeout(timer); },
  });
  return {window, state, opens, transactions, held, elements, revoked, blobs, events,
    expire: () => { for (const callback of [...deadlines]) callback(); }};
}

test('uses one Kinaiya namespaced database and waits for transaction commit', async () => {
  const h = harness({holdWrites: true});
  let finished = false;
  const write = h.window.kinaiyaPresentationWrite('snapshot-one').then(() => { finished = true; });
  await tick();
  assert.equal(finished, false);
  assert.equal(h.state.value, undefined);
  assert.deepEqual(h.opens, [['kinaiya-presentation-v1', 1]]);
  assert.deepEqual(h.transactions, [['snapshot', 'readwrite']]);
  h.held.shift()();
  await write;
  assert.equal(h.state.value, 'snapshot-one');
  assert.equal(h.state.closes, 1);
  assert.equal(await h.window.kinaiyaPresentationRead(), 'snapshot-one');
});

test('serializes writes so the latest snapshot is the durable result', async () => {
  const h = harness({holdWrites: true});
  const first = h.window.kinaiyaPresentationWrite('old');
  const last = h.window.kinaiyaPresentationWrite('new');
  await tick();
  assert.equal(h.held.length, 1);
  h.held.shift()();
  await first;
  await tick();
  assert.equal(h.held.length, 1);
  h.held.shift()();
  await last;
  assert.equal(h.state.value, 'new');
});

test('transaction failure leaves prior data intact and does not poison later writes', async () => {
  const options = {value: 'prior', failTransaction: true};
  const h = harness(options);
  await assert.rejects(h.window.kinaiyaPresentationWrite('rejected'), /Local presentation operation failed/);
  assert.equal(h.state.value, 'prior');
  options.failTransaction = false;
  await h.window.kinaiyaPresentationWrite('accepted');
  assert.equal(h.state.value, 'accepted');
});

test('denied, blocked and stalled IndexedDB fail with bounded generic errors', async () => {
  for (const options of [{openThrows: true}, {blocked: true}, {openStalls: true}]) {
    const h = harness(options);
    const assertion = assert.rejects(h.window.kinaiyaPresentationRead(), /Local presentation operation failed/);
    await tick();
    if (options.openStalls) h.expire();
    await assertion;
  }
  const h = harness({holdWrites: true, value: 'prior'});
  const assertion = assert.rejects(h.window.kinaiyaPresentationWrite('new'), /Local presentation operation failed/);
  await tick();
  h.expire();
  await assertion;
  h.held.shift()();
  assert.equal(h.state.value, 'prior');
  assert.equal(h.state.closes, 1);
});

test('snapshot read/write bounds use bytes and reject nonstrings', async () => {
  const h = harness();
  await assert.rejects(h.window.kinaiyaPresentationWrite({token: 'not presentation'}));
  await assert.rejects(h.window.kinaiyaPresentationWrite('é'.repeat((13 * 1024 * 1024 / 2) + 1)));
  assert.equal(h.opens.length, 0);
  const corrupt = harness({value: {unexpected: 'record'}});
  await assert.rejects(corrupt.window.kinaiyaPresentationRead());
});

test('picker rejects oversized files before reading and cleans its DOM seam', async () => {
  const h = harness();
  let reads = 0;
  const picked = h.window.kinaiyaPickCharacterPackage();
  const assertion = assert.rejects(picked, /Local presentation operation failed/);
  const input = h.elements[0];
  input.files = [{size: 12 * 1024 * 1024 + 1, text: () => { reads++; return '{}'; }}];
  await input.dispatch('change');
  await assertion;
  assert.equal(reads, 0);
  assert.equal(input.removed, true);
  assert.equal(h.events.size, 0);
});

test('picker cancellation, empty selection and focus-return cancellation resolve', async () => {
  for (const event of ['cancel', 'change', 'focus']) {
    const h = harness();
    const picked = h.window.kinaiyaPickCharacterPackage();
    if (event === 'focus') h.events.get('focus')();
    else await h.elements[0].dispatch(event);
    assert.equal(await picked, '');
    assert.equal(h.elements[0].removed, true);
  }
});

test('picker reads the selected local file and rejects encoded byte overrun', async () => {
  const h = harness();
  const picked = h.window.kinaiyaPickCharacterPackage();
  h.elements[0].files = [{size: 2, text: async () => '{}'}];
  await h.elements[0].dispatch('change');
  assert.equal(await picked, '{}');
  const bad = harness();
  const failed = assert.rejects(bad.window.kinaiyaPickCharacterPackage());
  bad.elements[0].files = [{size: 1, text: async () => 'é'.repeat(6 * 1024 * 1024 + 1)}];
  await bad.elements[0].dispatch('change');
  await failed;
});

test('a stalled selected file read fails within its bounded read window', async () => {
  const h = harness();
  const assertion = assert.rejects(h.window.kinaiyaPickCharacterPackage(), /Local presentation operation failed/);
  h.elements[0].files = [{size: 2, text: () => new Promise(() => {})}];
  h.elements[0].dispatch('change');
  h.expire();
  await assertion;
  assert.equal(h.elements[0].removed, true);
});

test('export uses fixed safe filename and always revokes its temporary Blob URL', async () => {
  const h = harness();
  await h.window.kinaiyaDownloadCharacterPackage('{"manifest":{"name":"../../unsafe"}}');
  assert.equal(h.elements[0].download, 'kinaiya-character-pack.json');
  assert.equal(h.elements[0].href, 'blob:local-test');
  assert.equal(h.elements[0].removed, true);
  assert.deepEqual(h.revoked, ['blob:local-test']);
  assert.equal(h.blobs[0].options.type, 'application/json;charset=utf-8');
  const failed = harness({onClick: () => { throw new Error('DOM private detail'); }});
  await assert.rejects(failed.window.kinaiyaDownloadCharacterPackage('{}'), /Local presentation operation failed/);
  assert.deepEqual(failed.revoked, ['blob:local-test']);
  assert.equal(failed.elements[0].removed, true);
});
