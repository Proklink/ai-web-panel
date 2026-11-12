import { useEffect, useState } from "react";
import { api, fmtTime, User } from "./api";

interface Dev { id: string; current: boolean; user_agent: string; ip: string; created_at: string; last_seen: string }

export default function Settings({ user, rightsCatalog, onToast }: { user: User; rightsCatalog: Record<string, string>; onToast: (s: string) => void }) {
  const [old, setOld] = useState("");
  const [p1, setP1] = useState("");
  const [p2, setP2] = useState("");
  const [devs, setDevs] = useState<Dev[]>([]);
  const load = () => api<Dev[]>("/me/sessions").then(setDevs).catch(() => {});
  useEffect(() => {
    load();
  }, []);

  async function change(e: React.FormEvent) {
    e.preventDefault();
    if (p1 !== p2) return onToast("Пароли не совпадают");
    try {
      await api("/me/password", { method: "POST", body: { old_password: old, new_password: p1 } });
      setOld(""); setP1(""); setP2("");
      onToast("Пароль изменён, остальные устройства разлогинены");
      load();
    } catch (e: any) {
      onToast(e.message);
    }
  }

  return (
    <div className="page narrow">
      <div className="card">
        <h3>{user.username}</h3>
        <div className="small muted">{user.is_admin ? "Администратор: все права, опасные действия — с подтверждением" : "Пользователь"}</div>
        {!user.is_admin && (
          <>
            <div className="small" style={{ marginTop: 8 }}>Права Claude в ваших диалогах:</div>
            <ul className="small">
              {user.rights.length === 0 && <li>только диалог, без доступа к файлам и командам</li>}
              {user.rights.map((r) => <li key={r}>{rightsCatalog[r] || r}</li>)}
            </ul>
            <div className="small muted">Проекты: {user.allowed_projects.join(", ") || "нет"} · лимит активных сессий: {user.max_sessions}</div>
          </>
        )}
      </div>
      <form className="card stack" onSubmit={change}>
        <h3>Сменить пароль</h3>
        <input type="password" placeholder="Текущий пароль" autoComplete="current-password" value={old} onChange={(e) => setOld(e.target.value)} />
        <input type="password" placeholder="Новый пароль (от 10 символов)" autoComplete="new-password" value={p1} onChange={(e) => setP1(e.target.value)} />
        <input type="password" placeholder="Повторите новый пароль" autoComplete="new-password" value={p2} onChange={(e) => setP2(e.target.value)} />
        <div className="row end"><button className="primary" disabled={!old || !p1}>Сменить</button></div>
      </form>
      <div className="card">
        <h3>Устройства, где выполнен вход</h3>
        <table className="table">
          <tbody>
            {devs.map((d) => (
              <tr key={d.id}>
                <td>
                  <div>{shortUA(d.user_agent)} {d.current && <span className="chip">это устройство</span>}</div>
                  <div className="small muted">{d.ip} · вход {fmtTime(d.created_at)} · активность {fmtTime(d.last_seen)}</div>
                </td>
                <td className="num">
                  {!d.current && (
                    <button className="small-btn" onClick={() => api(`/me/sessions/${d.id}`, { method: "DELETE" }).then(load)}>Выйти</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function shortUA(ua: string) {
  if (!ua) return "Неизвестно";
  const os = /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Windows/.test(ua) ? "Windows" : /Mac OS/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "";
  const br = /Edg\//.test(ua) ? "Edge" : /Firefox\//.test(ua) ? "Firefox" : /Chrome\//.test(ua) ? "Chrome" : /Safari\//.test(ua) ? "Safari" : ua.slice(0, 30);
  return [br, os].filter(Boolean).join(" · ");
}
