import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, Chat, fmtMb, fmtTime, Incident, KIND_LABEL, User } from "./api";
import { socket } from "./ws";
import Login from "./Login";
import ChatView from "./ChatView";
import Resources from "./Resources";
import Admin from "./Admin";
import Settings from "./Settings";

type Tab = "chat" | "resources" | "admin" | "settings";

export interface StartProblem {
  message: string;
  code: string;
  suggest: { id: string; title: string }[];
  retry: () => Promise<any>;
}

export type RunWithStart = (fn: () => Promise<any>) => Promise<boolean>;

export default function App() {
  const [status, setStatus] = useState<{
    setup_needed: boolean; user: User | null; rights_catalog: Record<string, string>;
    cert?: { name: string } | null; nocert_window_until?: number | null; access_error?: any;
  } | null>(null);
  const [chats, setChats] = useState<Chat[]>([]);
  const [active, setActive] = useState<string | null>(() => localStorageGet("wp-active"));
  const [tab, setTab] = useState<Tab>("chat");
  const [drawer, setDrawer] = useState(false);
  const [problem, setProblem] = useState<StartProblem | null>(null);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [showInc, setShowInc] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [online, setOnline] = useState(true);
  const [newOpen, setNewOpen] = useState(false);
  const user = status?.user ?? null;

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await api("/auth/status"));
    } catch (e: any) {
      setStatus({ setup_needed: false, user: null, rights_catalog: {} });
      setToast("Сервер недоступен: " + e.message);
    }
  }, []);

  useEffect(() => {
    loadStatus();
    const h = () => setStatus((s) => (s ? { ...s, user: null } : s));
    window.addEventListener("wp-unauthorized", h);
    return () => window.removeEventListener("wp-unauthorized", h);
  }, [loadStatus]);

  const loadChats = useCallback(async () => {
    const list = await api<Chat[]>("/chats");
    setChats(list);
  }, []);

  useEffect(() => {
    if (!user) {
      socket.stop();
      return;
    }
    socket.onStatus = setOnline;
    socket.start();
    loadChats().catch(() => {});
    api<Incident[]>("/incidents").then(setIncidents).catch(() => {});
    const off = socket.on((msg) => {
      if (msg.t === "chat") {
        setChats((cs) => {
          const i = cs.findIndex((c) => c.id === msg.chat.id);
          const next = i >= 0 ? cs.map((c) => (c.id === msg.chat.id ? msg.chat : c)) : [msg.chat, ...cs];
          return next.sort((a, b) => b.last_activity.localeCompare(a.last_activity));
        });
      } else if (msg.t === "chat_deleted") {
        setChats((cs) => cs.filter((c) => c.id !== msg.id));
        setActive((a) => (a === msg.id ? null : a));
      } else if (msg.t === "incident" && msg.incident.user_id === user.id) {
        setIncidents((xs) => [msg.incident, ...xs.filter((x) => x.id !== msg.incident.id)]);
        setToast(msg.incident.message);
      } else if (msg.t === "logout") {
        setStatus((s) => (s ? { ...s, user: null } : s));
      }
    });
    return off;
  }, [user?.id, loadChats]);

  useEffect(() => {
    localStorageSet("wp-active", active);
  }, [active]);

  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 8000);
    return () => clearTimeout(t);
  }, [toast]);

  // Запуск/возобновление сессии с обработкой лимитов и нехватки памяти
  const runWithStart: RunWithStart = useCallback(async (fn) => {
    try {
      await fn();
      return true;
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && typeof e.detail === "object" &&
          ["session_limit", "no_memory"].includes(e.detail.code)) {
        setProblem({ message: e.detail.message, code: e.detail.code, suggest: e.detail.suggest || [], retry: fn });
        return false;
      }
      setToast(e instanceof Error ? e.message : String(e));
      return false;
    }
  }, []);

  if (!status) return <div className="center muted">Загрузка…</div>;
  if (!user) return <Login setupNeeded={status.setup_needed} windowUntil={status.nocert_window_until ?? null} hasCert={!!status.cert}
    accessError={status.access_error} onLogin={() => loadStatus()} />;

  const unseen = incidents.filter((i) => !i.seen).length;
  const activeChat = chats.find((c) => c.id === active) || null;

  async function logout() {
    await api("/auth/logout", { method: "POST" }).catch(() => {});
    setStatus((s) => (s ? { ...s, user: null } : s));
    setChats([]);
  }

  return (
    <div className={"app" + (drawer ? " drawer-open" : "")}>
      <aside className="sidebar">
        <div className="sidebar-head">
          <div className="brand">
            <span className="logo" aria-hidden /> Webpanel
          </div>
          <button className="icon-btn only-mobile" onClick={() => setDrawer(false)} aria-label="Закрыть меню">✕</button>
        </div>
        <button className="primary new-chat" onClick={() => setNewOpen(true)}>+ Новый диалог</button>
        <nav className="chat-list" aria-label="Диалоги">
          {chats.length === 0 && <div className="muted small pad">Диалогов пока нет</div>}
          {chats.map((c) => (
            <ChatItem
              key={c.id}
              chat={c}
              active={c.id === active && tab === "chat"}
              onOpen={() => {
                setActive(c.id);
                setTab("chat");
                setDrawer(false);
              }}
              runWithStart={runWithStart}
              onToast={setToast}
            />
          ))}
        </nav>
        <div className="sidebar-foot">
          <button className="link small user-link" title="Профиль: пароль и устройства" onClick={() => { setTab("settings"); setDrawer(false); }}>
            {user.username}
            {user.is_admin && " · админ"}
            <span className="muted"> · активных {chats.filter((c) => c.state === "running").length}/{user.max_sessions}</span>
          </button>
          <button className="link small" onClick={logout}>Выйти</button>
        </div>
      </aside>
      <div className="scrim only-mobile" onClick={() => setDrawer(false)} />

      <main className="main">
        <header className="topbar">
          <button className="icon-btn only-mobile" onClick={() => setDrawer(true)} aria-label="Меню">☰</button>
          <div className="tabs" role="tablist">
            <TabBtn cur={tab} id="chat" set={setTab}>Чат</TabBtn>
            <TabBtn cur={tab} id="resources" set={setTab}>Ресурсы</TabBtn>
            {user.is_admin && <TabBtn cur={tab} id="admin" set={setTab}>Управление</TabBtn>}
            <TabBtn cur={tab} id="settings" set={setTab} className="only-desktop">Профиль</TabBtn>
          </div>
          <div className="spacer" />
          {!online && <span className="badge warn">нет связи</span>}
          <button
            className="icon-btn bell"
            aria-label="Уведомления"
            onClick={() => {
              setShowInc((v) => !v);
              if (unseen) {
                api("/incidents/seen", { method: "POST" }).catch(() => {});
                setIncidents((xs) => xs.map((x) => ({ ...x, seen: true })));
              }
            }}
          >
            🔔{unseen > 0 && <span className="bell-count">{unseen}</span>}
          </button>
        </header>
        {showInc && (
          <div className="incidents-pop card">
            <div className="row between">
              <strong>Уведомления</strong>
              <button className="icon-btn" onClick={() => setShowInc(false)}>✕</button>
            </div>
            {incidents.length === 0 && <div className="muted small">Пока ничего не случалось</div>}
            {incidents.map((i) => (
              <div key={i.id} className="incident">
                <div className="small muted">
                  {fmtTime(i.ts)} · {KIND_LABEL[i.kind] || i.kind}
                </div>
                <div>{i.message}</div>
              </div>
            ))}
          </div>
        )}

        {!status.cert && (
          <div className="banner nocert">
            Вы вошли без сертификата устройства{status.nocert_window_until ? ` — доступ до ${fmtTime(status.nocert_window_until)}` : ""}.
            {user.is_admin ? " Выпустите себе сертификат во вкладке «Управление»." : ""}
          </div>
        )}
        <section className="content">
          {tab === "chat" &&
            (activeChat ? (
              <ChatView key={activeChat.id} chat={activeChat} runWithStart={runWithStart} onToast={setToast} />
            ) : (
              <div className="empty">
                <h2>Выберите диалог или начните новый</h2>
                <p className="muted">Каждый диалог — отдельная сессия Claude в tmux. Она живёт, пока вы её не закроете или пока серверу хватает памяти.</p>
                <button className="primary" onClick={() => setNewOpen(true)}>+ Новый диалог</button>
              </div>
            ))}
          {tab === "resources" && <Resources user={user} />}
          {tab === "admin" && user.is_admin && <Admin me={user} rightsCatalog={status.rights_catalog} onToast={setToast} />}
          {tab === "settings" && <Settings user={user} rightsCatalog={status.rights_catalog} onToast={setToast} />}
        </section>
      </main>

      {newOpen && (
        <NewChatDialog
          onClose={() => setNewOpen(false)}
          runWithStart={runWithStart}
          onCreated={(c) => {
            setChats((cs) => [c, ...cs.filter((x) => x.id !== c.id)]);
            setActive(c.id);
            setTab("chat");
            setDrawer(false);
            setNewOpen(false);
          }}
        />
      )}

      {problem && (
        <Modal onClose={() => setProblem(null)} title={problem.code === "no_memory" ? "Не хватает памяти" : "Лимит сессий"}>
          <p>{problem.message}</p>
          {problem.suggest.length > 0 ? (
            <>
              <p className="muted small">Можно выгрузить на диск один из ваших активных диалогов (контекст сохранится) и продолжить:</p>
              <div className="stack">
                {problem.suggest.map((s) => (
                  <button
                    key={s.id}
                    onClick={async () => {
                      const p = problem;
                      setProblem(null);
                      try {
                        await api(`/chats/${s.id}/suspend`, { method: "POST" });
                      } catch (e: any) {
                        setToast(e.message);
                        return;
                      }
                      await runWithStart(p.retry);
                    }}
                  >
                    Выгрузить «{s.title}» и продолжить
                  </button>
                ))}
              </div>
            </>
          ) : (
            <p className="muted small">
              {problem.code === "no_memory"
                ? "Ваших активных диалогов нет — память занята другими процессами. Попробуйте позже или попросите администратора освободить ресурсы."
                : "Все ваши активные диалоги сейчас заняты ответом. Дождитесь окончания или остановите один из них."}
            </p>
          )}
        </Modal>
      )}

      {toast && (
        <div className="toast" role="status" onClick={() => setToast(null)}>
          {toast}
        </div>
      )}
    </div>
  );
}

function ChatItem({ chat: c, active, onOpen, runWithStart, onToast }: {
  chat: Chat; active: boolean; onOpen: () => void; runWithStart: RunWithStart; onToast: (s: string) => void;
}) {
  const suspended = c.state === "suspended";
  async function act(e: React.MouseEvent, fn: () => Promise<any>) {
    e.stopPropagation();
    try {
      await fn();
    } catch (err: any) {
      onToast(err.message);
    }
  }
  return (
    <div
      className={"chat-item" + (active ? " active" : "")}
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && e.target === e.currentTarget && (e.preventDefault(), onOpen())}
      title={c.title}
    >
      <div className="chat-item-main">
        <div className="chat-item-top">
          <StateDot chat={c} />
          <span className="chat-title">{c.title}</span>
          {c.approvals.length > 0 && <span className="badge warn" title="Ждёт подтверждения">!</span>}
        </div>
        <div className="chat-meta">
          <span title="Папка">📁 {c.project || "личная"}</span>
          <span title="Модель">{c.model || "модель по умолч."}</span>
          <span>{suspended ? "на диске" : c.busy ? "отвечает…" : c.rss_mb > 0 ? fmtMb(c.rss_mb) : "в RAM"}</span>
        </div>
      </div>
      <div className="chat-actions">
        {suspended ? (
          <button className="mini-btn" title="Вернуть в RAM" aria-label="Вернуть в RAM"
            onClick={(e) => { e.stopPropagation(); runWithStart(() => api(`/chats/${c.id}/resume`, { method: "POST" })); }}>▶</button>
        ) : (
          <button className="mini-btn" title="Выгрузить на диск (контекст сохранится)" aria-label="Выгрузить на диск" disabled={c.busy}
            onClick={(e) => act(e, () => api(`/chats/${c.id}/suspend`, { method: "POST" }))}>⏏</button>
        )}
        <button className="mini-btn danger" title="Удалить диалог" aria-label="Удалить диалог"
          onClick={(e) => {
            e.stopPropagation();
            if (confirm(`Удалить диалог «${c.title}» безвозвратно? Будут стёрты история, контекст Claude, вложения и файлы личной папки диалога.`))
              act(e, () => api(`/chats/${c.id}`, { method: "DELETE" }));
          }}>🗑</button>
      </div>
    </div>
  );
}

function StateDot({ chat }: { chat: Chat }) {
  if (chat.state === "suspended") return <span className="dot off" title="Выгружен на диск" />;
  if (chat.busy) return <span className="dot busy" title="Отвечает" />;
  return <span className="dot on" title="Активен в RAM" />;
}

function TabBtn({ cur, id, set, children, className = "" }: { cur: Tab; id: Tab; set: (t: Tab) => void; children: any; className?: string }) {
  return (
    <button role="tab" aria-selected={cur === id} className={"tab " + className + (cur === id ? " active" : "")} onClick={() => set(id)}>
      {children}
    </button>
  );
}

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: any }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [onClose]);
  return (
    <div className="modal-back" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal card" ref={ref} role="dialog" aria-label={title}>
        <div className="row between">
          <h3>{title}</h3>
          <button className="icon-btn" onClick={onClose} aria-label="Закрыть">✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

function NewChatDialog({ onClose, onCreated, runWithStart }: { onClose: () => void; onCreated: (c: Chat) => void; runWithStart: RunWithStart }) {
  const [projects, setProjects] = useState<string[]>([]);
  const [project, setProject] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api<string[]>("/projects").then(setProjects).catch(() => {});
  }, []);
  async function create() {
    setBusy(true);
    await runWithStart(async () => {
      const c = await api<Chat>("/chats", { method: "POST", body: { project: project || null, model: model || null } });
      onCreated(c);
    });
    setBusy(false);
  }
  return (
    <Modal title="Новый диалог" onClose={onClose}>
      <label>
        Проект (папка в workspace/)
        <select value={project} onChange={(e) => setProject(e.target.value)}>
          <option value="">Без проекта — личная папка диалога</option>
          {projects.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      </label>
      <label>
        Модель
        <select value={model} onChange={(e) => setModel(e.target.value)}>
          <option value="">По умолчанию</option>
          <option value="opus">Opus</option>
          <option value="sonnet">Sonnet</option>
          <option value="haiku">Haiku (быстрее, дешевле)</option>
        </select>
      </label>
      <div className="row end">
        <button onClick={onClose}>Отмена</button>
        <button className="primary" disabled={busy} onClick={create}>{busy ? "Запуск…" : "Создать"}</button>
      </div>
    </Modal>
  );
}

function localStorageGet(k: string): string | null {
  try {
    return localStorage.getItem(k);
  } catch {
    return null;
  }
}
function localStorageSet(k: string, v: string | null) {
  try {
    v ? localStorage.setItem(k, v) : localStorage.removeItem(k);
  } catch {
    /* приватный режим */
  }
}
