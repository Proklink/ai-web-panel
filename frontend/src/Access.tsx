import { useCallback, useEffect, useState } from "react";
import { api, fmtTime } from "./api";
import { Modal } from "./App";

interface Cert {
  id: number;
  name: string;
  fingerprint: string;
  user_id: number | null;
  username: string | null;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  last_used: string | null;
  last_ip: string | null;
}
interface AccessState { window_until: number | null; mode: string | null; configured: boolean; max_minutes: number }
interface Issued { cert: Cert; p12_b64: string; password: string; filename: string }

export default function Access({ users, onToast }: { users: { id: number; username: string }[]; onToast: (s: string) => void }) {
  const [certs, setCerts] = useState<Cert[]>([]);
  const [currentFp, setCurrentFp] = useState<string | null>(null);
  const [access, setAccess] = useState<AccessState | null>(null);
  const [name, setName] = useState("");
  const [userId, setUserId] = useState<string>("");
  const [days, setDays] = useState(365);
  const [issued, setIssued] = useState<Issued | null>(null);
  const [busy, setBusy] = useState(false);
  const err = (e: any) => onToast(e.message || String(e));

  const load = useCallback(() => {
    api<{ certs: Cert[]; current_fp: string | null }>("/admin/certs").then((r) => { setCerts(r.certs); setCurrentFp(r.current_fp); }).catch(err);
    api<AccessState>("/admin/access").then(setAccess).catch(err);
  }, []);
  useEffect(() => {
    load();
    const t = setInterval(() => api<AccessState>("/admin/access").then(setAccess).catch(() => {}), 15000);
    return () => clearInterval(t);
  }, [load]);

  async function issue(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const r = await api<Issued>("/admin/certs", { method: "POST", body: { name, user_id: userId ? Number(userId) : null, days } });
      download(r.p12_b64, r.filename);
      setIssued(r);
      setName("");
      load();
    } catch (e2) { err(e2); }
    setBusy(false);
  }

  async function setWindow(minutes: number) {
    try { setAccess(await api<AccessState>("/admin/access", { method: "POST", body: { minutes } })); } catch (e) { err(e); }
  }

  const open = access?.window_until && access.window_until * 1000 > Date.now();
  const noCert = !currentFp;
  const active = certs.filter((c) => !c.revoked_at);

  return (
    <div className="card">
      <h3>Доступ и сертификаты устройств</h3>
      <p className="small muted">
        Сервер принимает соединения только от устройств с сертификатом — без него не открывается даже страница входа,
        поэтому боты из интернета ничего не видят. Сертификат закрепляется за аккаунтом при первом входе.
      </p>

      {access && !access.configured && <div className="error">Caddy не настроен через ./webpanel.sh install — режимы доступа не применяются.</div>}
      <div className={"access-state " + (open ? "open" : "strict")}>
        <div>
          <strong>{open ? `Вход без сертификата открыт до ${fmtTime(access!.window_until!)}` : "Строгий режим: только с сертификатом"}</strong>
          <div className="small muted">
            {open ? "В это время без сертификата может войти только администратор. После закрытия такие входы завершатся."
              : "Если потеряете доступ со всех устройств — на сервере: sudo ./webpanel.sh open-window 30"}
          </div>
        </div>
        <div className="row gap">
          {open && <button onClick={() => setWindow(0)}>Закрыть сейчас</button>}
          <button disabled={noCert} title={noCert ? "Продлить можно только с устройства с сертификатом или с сервера" : ""} onClick={() => setWindow(15)}>
            {open ? "Продлить" : "Открыть"} на 15 мин
          </button>
          <button disabled={noCert} onClick={() => setWindow(60)}>на 60 мин</button>
        </div>
      </div>
      {noCert && (
        <div className="notice warn small" style={{ marginTop: 10 }}>
          Вы зашли без сертификата. Выпустите сертификат для этого устройства ниже, установите его и перезапустите браузер —
          сессия автоматически закрепится за сертификатом.
        </div>
      )}

      <form className="issue-form" onSubmit={issue}>
        <label>
          Устройство
          <input placeholder="например: телефон, ноутбук" value={name} onChange={(e) => setName(e.target.value)} maxLength={60} />
        </label>
        <label>
          Привязать к пользователю
          <select value={userId} onChange={(e) => setUserId(e.target.value)}>
            <option value="">к тому, кто первым войдёт с ним</option>
            {users.map((u) => <option key={u.id} value={u.id}>{u.username}</option>)}
          </select>
        </label>
        <label>
          Срок
          <select value={days} onChange={(e) => setDays(Number(e.target.value))}>
            <option value={90}>3 месяца</option>
            <option value={365}>1 год</option>
            <option value={730}>2 года</option>
          </select>
        </label>
        <button className="primary" disabled={!name.trim() || busy}>{busy ? "Выпуск…" : "Выпустить и скачать"}</button>
      </form>

      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>Устройство</th><th>Пользователь</th><th>Использован</th><th>Действует до</th><th /></tr></thead>
          <tbody>
            {certs.length === 0 && <tr><td colSpan={5} className="muted small">Сертификатов ещё нет</td></tr>}
            {certs.map((c) => (
              <tr key={c.id} className={c.revoked_at ? "revoked" : ""}>
                <td>
                  {c.name} {c.fingerprint === currentFp && <span className="chip">это устройство</span>}
                  <div className="small muted mono" title={c.fingerprint}>{c.fingerprint.slice(0, 16)}…</div>
                </td>
                <td>{c.username || <span className="muted small">не привязан</span>}</td>
                <td className="small">{c.last_used ? `${fmtTime(c.last_used)} · ${c.last_ip}` : <span className="muted">ещё нет</span>}</td>
                <td className="small">{c.revoked_at ? <span className="badge err">отозван {fmtTime(c.revoked_at)}</span> : fmtTime(c.expires_at)}</td>
                <td className="actions">
                  {!c.revoked_at && c.fingerprint !== currentFp && (
                    <button className="small-btn danger" onClick={async () => {
                      if (!confirm(`Отозвать сертификат «${c.name}»? Устройство сразу потеряет доступ.`)) return;
                      try { await api(`/admin/certs/${c.id}/revoke`, { method: "POST" }); load(); } catch (e) { err(e); }
                    }}>Отозвать</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="small muted">Активных: {active.length}. Отзыв действует мгновенно.</div>

      {issued && (
        <Modal title="Сертификат выпущен" onClose={() => setIssued(null)}>
          <p>Файл <code>{issued.filename}</code> скачан. Пароль для установки показывается <strong>один раз</strong>:</p>
          <div className="code-big">{issued.password}</div>
          <div className="row end">
            <button onClick={() => navigator.clipboard?.writeText(issued.password).then(() => onToast("Пароль скопирован"))}>Копировать пароль</button>
            <button onClick={() => download(issued.p12_b64, issued.filename)}>Скачать ещё раз</button>
          </div>
          <details open>
            <summary><strong>Как установить</strong></summary>
            <ul className="small install-help">
              <li><b>Android:</b> перекиньте файл на телефон → Настройки → Безопасность → Шифрование и учётные данные →
                Установить сертификат → <i>Сертификат пользователя VPN и приложений</i> → выберите файл, введите пароль.
                Chrome спросит, какой сертификат использовать, при первом заходе.</li>
              <li><b>iPhone/iPad:</b> откройте файл (AirDrop/Файлы) → Настройки → Профиль загружен → Установить, введите пароль.</li>
              <li><b>Windows:</b> двойной клик по файлу → Текущий пользователь → введите пароль → «Автоматически выбрать хранилище».</li>
              <li><b>macOS:</b> двойной клик → Связка ключей «вход» → пароль. Safari/Chrome спросят сертификат при заходе.</li>
              <li><b>Linux/Firefox:</b> Настройки → Приватность и защита → Сертификаты → Просмотр → Ваши сертификаты → Импортировать.</li>
            </ul>
            <p className="small muted">
              Чтобы браузер не ругался на самоподписанный HTTPS-сертификат сервера, установите ещё{" "}
              <a href="/api/admin/server-ca">сертификат сервера (CA)</a> как доверенный корневой.
              Файл .p12 после установки удалите из загрузок и мессенджеров.
            </p>
          </details>
        </Modal>
      )}
    </div>
  );
}

function download(b64: string, filename: string) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const url = URL.createObjectURL(new Blob([bytes], { type: "application/x-pkcs12" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}
