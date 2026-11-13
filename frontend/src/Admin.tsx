import { useCallback, useEffect, useState } from "react";
import { api, Chat, fmtMb, fmtTime, Incident, KIND_LABEL, User } from "./api";
import { socket } from "./ws";
import { Modal } from "./App";
import Access from "./Access";

type AUser = User & { chats: number; running: number };
type ASession = Chat & { username: string; user_id: number };
interface Invite { id: string; note: string | null; created_at: string; expires_at: string; used_by: string | null; used_at: string | null }
type SettingsT = Record<string, number>;

const SETTING_LABELS: Record<string, [string, string]> = {
  ram_reserve_mb: ["Резерв RAM для системы, МБ", "Ниже этого — простаивающие сессии выгружаются на диск"],
  ram_critical_mb: ["Критический порог, МБ", "Ниже этого — экстренно завершаются даже занятые сессии обычных пользователей"],
  session_estimate_mb: ["Оценка памяти на сессию, МБ", "Минимум; если реальные сессии крупнее, берётся среднее по ним"],
  default_max_sessions: ["Лимит сессий для новых пользователей", ""],
  approval_timeout_s: ["Ожидание подтверждения, с", "После — действие запрещается автоматически"],
  admin_confirm: ["Подтверждения для админа (1 — да, 0 — нет)", "0 — опасные действия админа выполняются без запроса. Обычных пользователей не касается"],
};

export default function Admin({ me, rightsCatalog, onToast }: { me: User; rightsCatalog: Record<string, string>; onToast: (s: string) => void }) {
  const [users, setUsers] = useState<AUser[]>([]);
  const [sessions, setSessions] = useState<ASession[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [settings, setSettings] = useState<SettingsT | null>(null);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [projects, setProjects] = useState<string[]>([]);
  const [newCode, setNewCode] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editProjects, setEditProjects] = useState<AUser | null>(null);

  const err = (e: any) => onToast(e.message || String(e));
  const loadUsers = useCallback(() => api<AUser[]>("/admin/users").then(setUsers).catch(err), []);
  const loadSessions = useCallback(() => api<ASession[]>("/admin/sessions").then(setSessions).catch(err), []);
  const loadInvites = useCallback(() => api<Invite[]>("/admin/invites").then(setInvites).catch(err), []);
  const loadIncidents = useCallback(() => api<Incident[]>("/admin/incidents").then(setIncidents).catch(err), []);

  useEffect(() => {
    loadUsers(); loadSessions(); loadInvites(); loadIncidents();
    api<SettingsT>("/admin/settings").then(setSettings).catch(err);
    api<string[]>("/projects").then(setProjects).catch(err);
    const off = socket.on((m) => {
      if (m.t === "sessions_changed") loadSessions();
      if (m.t === "incident") loadIncidents();
    });
    const t = setInterval(() => document.visibilityState === "visible" && loadSessions(), 5000);
    return () => { off(); clearInterval(t); };
  }, []);

  async function patch(u: AUser, body: any) {
    try {
      await api(`/admin/users/${u.id}`, { method: "PATCH", body });
      loadUsers();
    } catch (e) { err(e); }
  }

  async function sessionAction(s: ASession, action: "suspend" | "kill" | "delete") {
    const own = s.user_id === me.id;
    if (action === "kill" && !confirm(`Экстренно завершить сессию «${s.title}» (${s.username})? Текущий ответ будет прерван, пользователь увидит уведомление.`)) return;
    let reason: string | null = null;
    if (action === "delete") {
      reason = prompt(`Удалить диалог «${s.title}» (${s.username}) безвозвратно? ${own ? "" : "Пользователь получит уведомление."}\nПричина (необязательно):`, "");
      if (reason === null) return;
    }
    try {
      if (action === "delete") await api(`/admin/chats/${s.id}`, { method: "DELETE", body: { reason: reason || null } });
      else await api(`/admin/chats/${s.id}/${action}`, { method: "POST" });
      loadSessions(); loadIncidents();
    } catch (e) { err(e); }
  }

  return (
    <div className="page">
      <Access users={users} onToast={onToast} />
      <div className="card">
        <div className="row between">
          <h3>Активные сессии ({sessions.length})</h3>
          <span className="small muted">Все пользователи. Содержимое чужих диалогов недоступно — только управление.</span>
        </div>
        {sessions.length === 0 && <div className="muted small">Нет запущенных сессий</div>}
        <div className="table-wrap">
          <table className="table">
            <tbody>
              {sessions.map((s) => (
                <tr key={s.id}>
                  <td><strong>{s.username}</strong><div className="small muted">{s.title}</div></td>
                  <td className="num">{fmtMb(s.rss_mb)}</td>
                  <td>{s.busy ? <span className="badge busy">отвечает</span> : <span className="badge">ожидает</span>}</td>
                  <td className="actions">
                    <button className="small-btn" onClick={() => sessionAction(s, "suspend")} disabled={s.busy}>Выгрузить</button>
                    <button className="small-btn danger" onClick={() => sessionAction(s, "kill")}>Завершить</button>
                    <button className="small-btn danger" onClick={() => sessionAction(s, "delete")}>Удалить</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <div className="row between">
          <h3>Пользователи</h3>
          <div className="row gap">
            <button onClick={async () => {
              const note = prompt("Для какого устройства приглашение? (необязательно)", "") ;
              if (note === null) return;
              try {
                const r = await api<{ code: string }>("/admin/invites", { method: "POST", body: { note: note || null, hours: 24 } });
                setNewCode(r.code); loadInvites();
              } catch (e) { err(e); }
            }}>Приглашение</button>
            <button className="primary" onClick={() => setCreating(true)}>+ Пользователь</button>
          </div>
        </div>
        <div className="small muted">Изменение прав вступает в силу сразу для ограничений и после перезапуска сессии — для новых инструментов.</div>
        <div className="table-wrap">
          <table className="table users">
            <thead>
              <tr><th>Логин</th>{Object.keys(rightsCatalog).map((r) => <th key={r} title={rightsCatalog[r]}>{r}</th>)}<th>Сессий</th><th>Проекты</th><th /></tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.id}>
                  <td>
                    <strong>{u.username}</strong>{u.is_admin && <span className="chip">админ</span>}
                    <div className="small muted">чатов {u.chats}, активно {u.running} · {u.last_login ? "вход " + fmtTime(u.last_login) : "не входил"}</div>
                  </td>
                  {Object.keys(rightsCatalog).map((r) => (
                    <td key={r} className="center-cell">
                      {u.is_admin ? "✓" : (
                        <input type="checkbox" aria-label={rightsCatalog[r]} checked={u.rights.includes(r)} onChange={(e) => {
                          if (r === "bash" && e.target.checked && !confirm("Право Bash даёт Claude выполнять команды от root. Деструктивные команды будут заблокированы, но это не полноценная изоляция. Выдать?")) return;
                          patch(u, { rights: e.target.checked ? [...u.rights, r] : u.rights.filter((x) => x !== r) });
                        }} />
                      )}
                    </td>
                  ))}
                  <td>
                    <input className="num-input" type="number" min={0} max={20} defaultValue={u.max_sessions}
                      onBlur={(e) => Number(e.target.value) !== u.max_sessions && patch(u, { max_sessions: Number(e.target.value) })} />
                  </td>
                  <td>
                    {u.is_admin ? <span className="small muted">все</span> : (
                      <button className="small-btn" onClick={() => setEditProjects(u)}>{u.allowed_projects.length ? u.allowed_projects.join(", ") : "нет"}</button>
                    )}
                  </td>
                  <td className="actions">
                    <button className="small-btn" onClick={async () => {
                      const p = prompt(`Новый пароль для ${u.username} (от 10 символов):`);
                      if (!p) return;
                      try { await api(`/admin/users/${u.id}/password`, { method: "POST", body: { password: p } }); onToast("Пароль изменён"); } catch (e) { err(e); }
                    }}>Пароль</button>
                    {!u.is_admin && (
                      <>
                        <button className="small-btn" onClick={() => api(`/admin/users/${u.id}/logout`, { method: "POST" }).then(() => onToast("Все устройства пользователя разлогинены")).catch(err)}>Разлогинить</button>
                        <button className="small-btn danger" onClick={async () => {
                          if (!confirm(`Удалить пользователя ${u.username} и ВСЕ его диалоги безвозвратно?`)) return;
                          try { await api(`/admin/users/${u.id}`, { method: "DELETE" }); loadUsers(); loadSessions(); } catch (e) { err(e); }
                        }}>Удалить</button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {invites.length > 0 && (
          <details>
            <summary className="small">Приглашения ({invites.filter((i) => !i.used_by).length} неиспользованных)</summary>
            <table className="table">
              <tbody>
                {invites.map((i) => (
                  <tr key={i.id}>
                    <td>{i.note || "—"}</td>
                    <td className="small">{i.used_by ? `использовал ${i.used_by}` : new Date(i.expires_at) < new Date() ? "истекло" : `до ${fmtTime(i.expires_at)}`}</td>
                    <td className="num"><button className="small-btn" onClick={() => api(`/admin/invites/${i.id}`, { method: "DELETE" }).then(loadInvites)}>✕</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        )}
      </div>

      {settings && (
        <div className="card">
          <h3>Ресурсы и лимиты</h3>
          <form className="settings-grid" onSubmit={async (e) => {
            e.preventDefault();
            try { setSettings(await api("/admin/settings", { method: "PUT", body: settings })); onToast("Сохранено"); } catch (e2) { err(e2); }
          }}>
            {Object.keys(SETTING_LABELS).map((k) => (
              <label key={k}>
                {SETTING_LABELS[k][0]}
                <input type="number" value={settings[k]} onChange={(e) => setSettings({ ...settings, [k]: Number(e.target.value) })} />
                {SETTING_LABELS[k][1] && <span className="small muted">{SETTING_LABELS[k][1]}</span>}
              </label>
            ))}
            <div className="row end full"><button className="primary">Сохранить</button></div>
          </form>
        </div>
      )}

      <div className="card">
        <h3>Журнал событий</h3>
        {incidents.length === 0 && <div className="muted small">Пусто</div>}
        <div className="table-wrap">
          <table className="table">
            <tbody>
              {incidents.map((i) => (
                <tr key={i.id}>
                  <td className="small nowrap">{fmtTime(i.ts)}</td>
                  <td><span className={"badge " + (["emergency", "crash", "admin_kill"].includes(i.kind) ? "err" : "warn")}>{KIND_LABEL[i.kind] || i.kind}</span></td>
                  <td className="small">{i.username || "—"}</td>
                  <td className="small">
                    {i.message}
                    {i.details && Object.keys(i.details).length > 0 && (
                      <div className="muted">{Object.entries(i.details).filter(([k]) => k !== "stderr").map(([k, v]) => `${k}: ${v}`).join(" · ")}</div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {newCode && (
        <Modal title="Код приглашения" onClose={() => setNewCode(null)}>
          <p>Код показывается один раз и действует 24 часа. Введите его на новом устройстве в форме «Зарегистрировать устройство».</p>
          <div className="code-big">{newCode}</div>
          <div className="row end"><button onClick={() => navigator.clipboard?.writeText(newCode).then(() => onToast("Скопировано"))}>Копировать</button></div>
        </Modal>
      )}
      {creating && <CreateUser rightsCatalog={rightsCatalog} projects={projects} defMax={settings?.default_max_sessions ?? 2}
        onClose={() => setCreating(false)} onDone={() => { setCreating(false); loadUsers(); }} onErr={err} />}
      {editProjects && (
        <Modal title={`Проекты для ${editProjects.username}`} onClose={() => setEditProjects(null)}>
          <ProjectPicker projects={projects} value={editProjects.allowed_projects} onSave={(v) => { patch(editProjects, { allowed_projects: v }); setEditProjects(null); }} />
        </Modal>
      )}
    </div>
  );
}

function ProjectPicker({ projects, value, onSave }: { projects: string[]; value: string[]; onSave: (v: string[]) => void }) {
  const [sel, setSel] = useState<string[]>(value);
  return (
    <>
      <div className="stack">
        {projects.length === 0 && <div className="muted small">В workspace нет папок</div>}
        {projects.map((p) => (
          <label key={p} className="check">
            <input type="checkbox" checked={sel.includes(p)} onChange={(e) => setSel(e.target.checked ? [...sel, p] : sel.filter((x) => x !== p))} />
            {p}
          </label>
        ))}
      </div>
      <div className="row end"><button className="primary" onClick={() => onSave(sel)}>Сохранить</button></div>
    </>
  );
}

function CreateUser({ rightsCatalog, projects, defMax, onClose, onDone, onErr }: {
  rightsCatalog: Record<string, string>; projects: string[]; defMax: number; onClose: () => void; onDone: () => void; onErr: (e: any) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [rights, setRights] = useState<string[]>([]);
  const [maxS, setMaxS] = useState(defMax);
  return (
    <Modal title="Новый пользователь" onClose={onClose}>
      <form className="stack" onSubmit={async (e) => {
        e.preventDefault();
        try { await api("/admin/users", { method: "POST", body: { username, password, rights, max_sessions: maxS, allowed_projects: [] } }); onDone(); } catch (e2) { onErr(e2); }
      }}>
        <input placeholder="Логин (например, phone)" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" />
        <input placeholder="Пароль (от 10 символов)" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
        <div className="small muted">Права (по умолчанию — только диалог):</div>
        {Object.entries(rightsCatalog).map(([r, label]) => (
          <label key={r} className="check">
            <input type="checkbox" checked={rights.includes(r)} onChange={(e) => setRights(e.target.checked ? [...rights, r] : rights.filter((x) => x !== r))} />
            {label}
          </label>
        ))}
        <label>Лимит активных сессий<input type="number" min={0} max={20} value={maxS} onChange={(e) => setMaxS(Number(e.target.value))} /></label>
        {projects.length > 0 && <div className="small muted">Доступ к проектам можно настроить после создания.</div>}
        <div className="row end"><button type="button" onClick={onClose}>Отмена</button><button className="primary" disabled={!username || !password}>Создать</button></div>
      </form>
    </Modal>
  );
}
