// AC35: the ingest token lives in browser.storage.local; background.js sends it as X-Token.
const input = document.getElementById('token');
browser.storage.local.get('token').then(({ token }) => { input.value = token || ''; });
document.getElementById('save').addEventListener('click', async () => {
  await browser.storage.local.set({ token: input.value.trim() });
  document.getElementById('msg').textContent = 'Saved.';
});
