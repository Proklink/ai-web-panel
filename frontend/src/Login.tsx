import { useState } from "react";
import { api, fmtTime, User } from "./api";

type Mode = "login" | "register" | "setup";

export default function Login({ setupNeeded, onLogin, windowUntil, hasCert, accessError }: {
  setupNeeded: boolean; onLogin: (u: User) => void; windowUntil: number | null; hasCert: boolean; accessError?: any;
}) {
  const [mode, setMode] = useState<Mode>(setupNeeded ? "setup" : "login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [password2, setPassword2] = useState("");
  const [code, setCode] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr("");
    if (mode !== "login" && password !== password2) return setErr("Пароли не совпадают");
    setBusy(true);
    try {
      const body: any = { username, password };
      if (mode === "setup") body.setup_code = code;
      if (mode === "register") body.invite = code;
      const r = await api<{ user: User }>(`/auth/${mode}`, { method: "POST", body });
      onLogin(r.user);
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  if (accessError) {
    return (
      <div className="login-wrap">
        <div className="login card">
          <div className="brand"><span className="logo" aria-hidden /> Webpanel</div>
          <h1>Доступ закрыт</h1>
          <div className="error">{accessError.message || String(accessError)}</div>
        </div>
      </div>
    );
  }

  return (
    <div className="login-wrap">
      <form className="login card" onSubmit={submit}>
        <div className="brand">
          <span className="logo" aria-hidden /> Webpanel
        </div>
        {!hasCert && windowUntil && (
          <div className="notice warn small">Вход без сертификата устройства временно открыт до {fmtTime(windowUntil)} — только для администратора.</div>
        )}
        <h1>{mode === "setup" ? "Первичная настройка" : mode === "register" ? "Новое устройство" : "Вход"}</h1>
        {mode === "setup" && (
          <p className="muted small">
            Создаётся администратор. Код настройки лежит на сервере: <code>sudo cat /etc/webpanel/setup-code</code> или{" "}
            <code>./webpanel.sh setup-code</code>
          </p>
        )}
        {mode === "register" && <p className="muted small">Код приглашения выдаёт администратор во вкладке «Управление».</p>}
        <label>
          Логин
          <input autoFocus autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} />
        </label>
        <label>
          Пароль
          <input
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        {mode !== "login" && (
          <>
            <label>
              Повторите пароль
              <input type="password" autoComplete="new-password" value={password2} onChange={(e) => setPassword2(e.target.value)} />
            </label>
            <label>
              {mode === "setup" ? "Код настройки" : "Код приглашения"}
              <input value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" />
            </label>
          </>
        )}
        {err && <div className="error">{err}</div>}
        <button className="primary" disabled={busy || !username || !password}>
          {mode === "login" ? "Войти" : "Создать и войти"}
        </button>
        {!setupNeeded && (
          <button type="button" className="link" onClick={() => setMode(mode === "login" ? "register" : "login")}>
            {mode === "login" ? "Есть код приглашения? Зарегистрировать устройство" : "Уже есть аккаунт? Войти"}
          </button>
        )}
      </form>
    </div>
  );
}
