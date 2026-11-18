/* Owner-selected local file import/export. No credential logging or network calls. */
window.kinaiyaChooseCredential = () => new Promise(resolve => {
  const input = document.createElement('input');
  input.type = 'file'; input.accept = '.txt,.json';
  input.addEventListener('cancel', () => resolve(''), {once: true});
  input.addEventListener('change', async () => {
    try {
      const file = input.files?.[0];
      if (!file || file.size > 4096) return resolve('');
      const text = (await file.text()).trim();
      let token = text;
      if (text.startsWith('{')) {
        const parsed = JSON.parse(text);
        token = parsed.owner || parsed.code || parsed.auth_code || text;
      }
      resolve(typeof token === 'string' && /^[A-Za-z0-9_.-]{20,256}$/.test(token) ? token : '');
    } catch (_) { resolve(''); }
  }, {once: true});
  input.click();
});

window.kinaiyaExportCredential = (filename, content) => {
  if (!content || typeof content !== 'string') return;
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename || 'owner-login.txt';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
};

// IndexedDB is origin-scoped, including the port. This store accepts only the
// revocable device proof. Session bearers and owner exports stay in memory.
window.kinaiyaDeviceProof = (value) => new Promise(resolve => {
  const valid = proof => typeof proof === 'string' && /^kny_refr_[A-Za-z0-9_-]{32,128}$/.test(proof);
  if (value != null && !valid(value)) { resolve(''); return; }
  let database, transaction, settled = false;
  const finish = (result = '') => {
    settled = true;
    clearTimeout(timer);
    database?.close();
    resolve(result);
  };
  // A blocked upgrade or storage denial must leave a fresh launch usable.
  const timer = setTimeout(() => {
    try { transaction?.abort(); } catch (_) { /* already complete */ }
    finish();
  }, 1500);
  try {
    const open = window.indexedDB.open('kinaiya-device-v1', 1);
    open.onupgradeneeded = () => open.result.createObjectStore('pairing');
    open.onerror = open.onblocked = () => finish();
    open.onsuccess = () => {
      database = open.result;
      if (settled) { database.close(); return; }
      try {
        transaction = database.transaction('pairing', value == null ? 'readonly' : 'readwrite');
        const store = transaction.objectStore('pairing');
        const request = value == null ? store.get('device') : store.put(value, 'device');
        transaction.oncomplete = () => finish(value == null && valid(request.result) ? request.result : '');
        transaction.onabort = transaction.onerror = () => finish();
      } catch (_) { finish(); }
    };
  } catch (_) { finish(); }
});
