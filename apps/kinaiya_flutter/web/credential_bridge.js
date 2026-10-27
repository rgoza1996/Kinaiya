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
      if (text.startsWith('{')) token = JSON.parse(text).owner;
      resolve(typeof token === 'string' && /^[A-Za-z0-9_.-]{20,256}$/.test(token) ? token : '');
    } catch (_) { resolve(''); }
  }, {once: true});
  input.click();
});
