/* Owner-selected local file only. No network, storage, logging or implicit reads. */
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
