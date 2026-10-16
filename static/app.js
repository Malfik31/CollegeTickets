// Клиентская логика: контроль входа (fetch), подтверждения, защита от двойной отправки.
const csrf = document.querySelector('meta[name=csrf]')?.content;

document.addEventListener('submit', e => {
  const f = e.target;
  if (f.dataset.confirm && !confirm(f.dataset.confirm)) return e.preventDefault();
  if (f.id === 'checkin') return;
  setTimeout(() => f.querySelectorAll('button:not(.link)').forEach(b => b.disabled = true), 0);
});
document.querySelectorAll('form[data-autosubmit] select').forEach(s => s.addEventListener('change', () => s.form.submit()));

const form = document.getElementById('checkin');
if (form) {
  const box = document.getElementById('result'), input = form.token;
  const setStats = s => { if (s) { document.getElementById('s-reg').textContent = s.reg; document.getElementById('s-vis').textContent = s.visited; } };
  const show = (ok, text) => { box.hidden = false; box.className = 'flash ' + (ok ? 'ok' : 'error'); box.textContent = text; };
  form.addEventListener('submit', async e => {
    e.preventDefault();
    const token = input.value.trim();
    if (!token) return show(false, 'Введите токен билета');
    const btn = form.querySelector('button'); btn.disabled = true;
    try {
      const r = await fetch(form.dataset.url, { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRF': csrf },
        body: JSON.stringify({ token }) });
      const d = await r.json();
      show(r.ok, r.ok ? `✔ Вход разрешён: ${d.name}` : d.error);
      setStats(d.stats);
    } catch { show(false, 'Нет связи с сервером'); }
    btn.disabled = false; input.value = ''; input.focus();
  });
  fetch(form.dataset.url.replace('check-in', 'attendees'), { headers: { Accept: 'text/html' } }).then(r => r.text()).then(html => {
    const d = new DOMParser().parseFromString(html, 'text/html');
    setStats({ reg: d.getElementById('s-reg')?.textContent, visited: d.getElementById('s-vis')?.textContent });
  });
}
