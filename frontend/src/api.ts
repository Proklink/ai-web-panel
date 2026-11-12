export class ApiError extends Error {
  status: number;
  detail: any;
  constructor(status: number, detail: any) {
    super(typeof detail === "string" ? detail : detail?.message || `Ошибка ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

export async function api<T = any>(path: string, opts: { method?: string; body?: any } = {}): Promise<T> {
  const res = await fetch("/api" + path, {
    method: opts.method || "GET",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Requested-With": "webpanel" },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  let data: any = null;
  try {
    data = await res.json();
  } catch {
    /* пустой ответ */
  }
  if (!res.ok) {
    let detail = data?.detail ?? `Ошибка ${res.status}`;
    if (Array.isArray(detail)) detail = detail.map((d: any) => d.msg).join("; ");
    if (res.status === 401) window.dispatchEvent(new Event("wp-unauthorized"));
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

export interface User {
  id: number;
  username: string;
  is_admin: boolean;
  rights: string[];
  max_sessions: number;
  allowed_projects: string[];
  created_at: string;
  last_login: string | null;
}

export interface Approval {
  id: string;
  chat_id: string;
  tool: string;
  input: any;
  reason: string;
  created: number;
}

export interface Chat {
  id: string;
  title: string;
  project: string | null;
  model: string | null;
  state: "running" | "suspended";
  busy: boolean;
  notice: string | null;
  created_at: string;
  last_activity: string;
  approvals: Approval[];
  rss_mb: number;
  has_transcript: boolean;
}

export interface Ev {
  seq: number;
  ts: number;
  k: "user" | "claude" | "sys";
  d: any;
}

export interface Incident {
  id: number;
  ts: string;
  kind: string;
  chat_id: string | null;
  chat_title: string | null;
  message: string;
  details: any;
  seen: boolean;
  username?: string;
}

export const KIND_LABEL: Record<string, string> = {
  auto_suspend: "Выгрузка из RAM",
  emergency: "Экстренное завершение",
  admin_kill: "Завершено админом",
  admin_suspend: "Выгружено админом",
  admin_delete: "Удалено админом",
  crash: "Сбой процесса",
};

export function fmtTime(iso: string | number) {
  const d = typeof iso === "number" ? new Date(iso * 1000) : new Date(iso);
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

export function fmtMb(mb: number) {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} ГБ` : `${Math.round(mb)} МБ`;
}
