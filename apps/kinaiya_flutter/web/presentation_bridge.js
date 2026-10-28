/* Presentation-only local files and one transactional local snapshot. No network. */
(() => {
  'use strict';
  const PACKAGE_LIMIT = 12 * 1024 * 1024;
  const SNAPSHOT_LIMIT = 13 * 1024 * 1024;
  const DB_NAME = 'kinaiya-presentation-v1';
  const STORE = 'snapshot';
  const KEY = 'current';
  const failure = () => new Error('Local presentation operation failed.');
  const bounded = (value, limit) => typeof value === 'string' &&
    value.length <= limit && new TextEncoder().encode(value).length <= limit;

  function openDatabase() {
    return new Promise((resolve, reject) => {
      let settled = false;
      let request;
      const finish = (error, database) => {
        if (settled) { database?.close(); return; }
        settled = true;
        clearTimeout(timer);
        error ? reject(failure()) : resolve(database);
      };
      const timer = setTimeout(() => finish(true), 5000);
      try {
        request = window.indexedDB.open(DB_NAME, 1);
        request.onupgradeneeded = () => {
          try {
            if (!request.result.objectStoreNames.contains(STORE)) {
              request.result.createObjectStore(STORE);
            }
          } catch (_) { finish(true); }
        };
        request.onsuccess = () => finish(false, request.result);
        request.onerror = request.onblocked = () => finish(true);
      } catch (_) { finish(true); }
    });
  }

  async function transaction(write, value) {
    const database = await openDatabase();
    try {
      return await new Promise((resolve, reject) => {
        let result = '';
        let settled = false;
        let tx;
        const finish = (error) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          error ? reject(failure()) : resolve(result);
        };
        const timer = setTimeout(() => {
          try { tx?.abort(); } catch (_) { /* already complete */ }
          finish(true);
        }, 5000);
        try {
          tx = database.transaction(STORE, write ? 'readwrite' : 'readonly');
          tx.oncomplete = () => finish(false);
          tx.onerror = tx.onabort = () => finish(true);
          const store = tx.objectStore(STORE);
          const request = write ? store.put(value, KEY) : store.get(KEY);
          request.onerror = () => { try { tx.abort(); } catch (_) { finish(true); } };
          if (!write) request.onsuccess = () => {
            const saved = request.result;
            if (saved === undefined) return;
            if (!bounded(saved, SNAPSHOT_LIMIT)) {
              try { tx.abort(); } catch (_) { finish(true); }
            } else { result = saved; }
          };
        } catch (_) { finish(true); }
      });
    } finally { database.close(); }
  }

  let writes = Promise.resolve();
  window.kinaiyaPresentationRead = () => transaction(false);
  window.kinaiyaPresentationWrite = (value) => {
    if (!bounded(value, SNAPSHOT_LIMIT)) return Promise.reject(failure());
    const next = writes.then(() => transaction(true, value));
    writes = next.catch(() => {});
    return next;
  };

  window.kinaiyaPickCharacterPackage = () => new Promise((resolve, reject) => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json,application/json';
    input.style.display = 'none';
    let settled = false;
    let reading = false;
    let focusTimer;
    let readTimer;
    const onFocus = () => {
      focusTimer = setTimeout(() => {
        if (!reading && !input.files?.length) finish(false, '');
      }, 250);
    };
    const finish = (error, value = '') => {
      if (settled) return;
      settled = true;
      clearTimeout(focusTimer);
      clearTimeout(readTimer);
      window.removeEventListener('focus', onFocus);
      input.remove();
      error ? reject(failure()) : resolve(value);
    };
    input.addEventListener('cancel', () => finish(false), {once: true});
    input.addEventListener('change', async () => {
      reading = true;
      try {
        const file = input.files?.[0];
        if (!file) return finish(false);
        // Inspect the byte count before asking the browser to materialize text.
        if (!Number.isSafeInteger(file.size) || file.size > PACKAGE_LIMIT || file.size < 0) {
          return finish(true);
        }
        readTimer = setTimeout(() => finish(true), 5000);
        const value = await file.text();
        if (!bounded(value, PACKAGE_LIMIT)) return finish(true);
        finish(false, value);
      } catch (_) { finish(true); }
    }, {once: true});
    window.addEventListener('focus', onFocus, {once: true});
    try { document.body.appendChild(input); input.click(); }
    catch (_) { finish(true); }
  });

  window.kinaiyaDownloadCharacterPackage = async (value) => {
    if (!bounded(value, PACKAGE_LIMIT)) throw failure();
    let url;
    let anchor;
    try {
      const blob = new Blob([value], {type: 'application/json;charset=utf-8'});
      url = URL.createObjectURL(blob);
      anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = 'kinaiya-character-pack.json';
      anchor.style.display = 'none';
      document.body.appendChild(anchor);
      anchor.click();
      // Keep the URL until the click's navigation task can consume it.
      await new Promise(resolve => setTimeout(resolve, 0));
    } catch (_) { throw failure(); }
    finally { anchor?.remove(); if (url) URL.revokeObjectURL(url); }
  };
})();
