import { memo, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, Approval, Chat, Ev, fmtSize, fmtTime, UploadedFile, uploadFile, uploadUrl } from "./api";
import { socket } from "./ws";
import type { RunWithStart } from "./App";

type Item =
  | { kind: "user"; key: string; text: string; ts: number; files?: UploadedFile[] }
  | { kind: "text"; key: string; text: string }
  | { kind: "thinking"; key: string; text: string }
  | { kind: "tool"; key: string; id: string; name: string; input: any; result?: { content: any; is_error: boolean } }
  | { kind: "result"; key: string; d: any }
  | { kind: "notice"; key: string; level: string; text: string; ts: number }
  | { kind: "approval"; key: string; a: any; outcome?: { decision: string; reason: string } };

function buildItems(events: Ev[]): Item[] {
  const items: Item[] = [];
  const tools = new Map<string, Item & { kind: "tool" }>();
  const approvals = new Map<string, Item & { kind: "approval" }>();
  for (const ev of events) {
    const d = ev.d || {};
    if (ev.k === "user") items.push({ kind: "user", key: "u" + ev.seq, text: d.text, ts: ev.ts, files: d.files });
    else if (ev.k === "sys") {
      if (d.event === "notice") items.push({ kind: "notice", key: "n" + ev.seq, level: d.level || "info", text: d.text, ts: ev.ts });
      else if (d.event === "exited" && d.code !== 0 && d.code !== null)
        items.push({ kind: "notice", key: "x" + ev.seq, level: "error", ts: ev.ts,
          text: `Процесс Claude завершился с кодом ${d.code}.` + (d.stderr || d.error ? "\n\n```\n" + String(d.stderr || d.error).slice(-1200) + "\n```" : "") });
      else if (d.event === "approval_request") {
        const it = { kind: "approval" as const, key: "a" + ev.seq, a: d };
        approvals.set(d.id, it);
        items.push(it);
      } else if (d.event === "approval_result") {
        const it = approvals.get(d.id);
        if (it) it.outcome = { decision: d.decision, reason: d.reason };
      }
    } else if (ev.k === "claude") {
      const t = d.type;
      if (t === "assistant") {
        if (d.parent_tool_use_id) continue; // события субагентов не показываем отдельно
        (d.message?.content || []).forEach((b: any, i: number) => {
          const key = `c${ev.seq}-${i}`;
          if (b.type === "text" && b.text?.trim()) items.push({ kind: "text", key, text: b.text });
          else if (b.type === "thinking" && b.thinking?.trim()) items.push({ kind: "thinking", key, text: b.thinking });
          else if (b.type === "tool_use") {
            const it = { kind: "tool" as const, key, id: b.id, name: b.name, input: b.input };
            tools.set(b.id, it);
            items.push(it);
          }
        });
      } else if (t === "user") {
        const content = d.message?.content;
        if (Array.isArray(content))
          for (const b of content)
            if (b.type === "tool_result") {
              const it = tools.get(b.tool_use_id);
              if (it) it.result = { content: b.content, is_error: !!b.is_error };
            }
      } else if (t === "result") items.push({ kind: "result", key: "r" + ev.seq, d });
      else if (t === "system" && d.subtype === "api_retry")
        items.push({ kind: "notice", key: "s" + ev.seq, level: "warn", ts: ev.ts, text: `Повтор запроса к API (попытка ${d.attempt ?? "?"})…` });
      else if (t === "rate_limit_event" && d.rate_limit_info?.status && d.rate_limit_info.status !== "allowed")
        items.push({ kind: "notice", key: "l" + ev.seq, level: "warn", ts: ev.ts, text: `Лимит использования: ${d.rate_limit_info.status}` });
    }
  }
  return items;
}

const finePointer = typeof window !== "undefined" && window.matchMedia("(pointer: fine)").matches;
const MAX_FILES = 10;
const MAX_MB = 25;
const TALL_KEY = "wp-composer-tall";

type Att = {
  key: string;
  name: string;
  size: number;
  preview?: string;
  progress: number;
  status: "uploading" | "done" | "error";
  error?: string;
  file?: UploadedFile;
  abort?: () => void;
};

/** Большие фото (и HEIC) пережимаем в JPEG ≤2560 px: быстрее загрузка, Claude принимает картинки до 5 МБ. */
async function prepareImage(f: File): Promise<{ blob: Blob; name: string }> {
  const heic = /image\/hei[cf]/.test(f.type) || /\.hei[cf]$/i.test(f.name);
  if (!f.type.startsWith("image/") || f.type === "image/gif" || f.type === "image/svg+xml") return { blob: f, name: f.name };
  if (!heic && f.size <= 3.5 * 1024 * 1024) return { blob: f, name: f.name };
  try {
    const bmp = await createImageBitmap(f);
    const k = Math.min(1, 2560 / Math.max(bmp.width, bmp.height));
    const c = document.createElement("canvas");
    c.width = Math.round(bmp.width * k);
    c.height = Math.round(bmp.height * k);
    c.getContext("2d")!.drawImage(bmp, 0, 0, c.width, c.height);
    bmp.close?.();
    const blob = await new Promise<Blob | null>((r) => c.toBlob(r, "image/jpeg", 0.88));
    if (!blob) throw new Error("toBlob");
    return { blob, name: f.name.replace(/\.[^.]+$/, "") + ".jpg" };
  } catch {
    return { blob: f, name: f.name };
  }
}

export default function ChatView({ chat, runWithStart, onToast }: { chat: Chat; runWithStart: RunWithStart; onToast: (s: string) => void }) {
  const [events, setEvents] = useState<Ev[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [draft, setDraft] = useState("");
  const [live, setLive] = useState("");
  const [sending, setSending] = useState(false);
  const seqRef = useRef(0);
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [atts, setAtts] = useState<Att[]>([]);
  const [drag, setDrag] = useState(false);
  const [tall, setTall] = useState(() => localStorage.getItem(TALL_KEY) === "1");

  function toggleTall() {
    const v = !tall;
    setTall(v);
    localStorage.setItem(TALL_KEY, v ? "1" : "0");
    if (taRef.current) taRef.current.style.height = ""; // сбросить ручную высоту
    taRef.current?.focus();
  }

  function patchAtt(key: string, p: Partial<Att>) {
    setAtts((as) => as.map((a) => (a.key === key ? { ...a, ...p } : a)));
  }

  async function addFiles(list: FileList | File[]) {
    const files = Array.from(list);
    if (!files.length) return;
    const room = MAX_FILES - atts.length;
    if (files.length > room) onToast(`Не больше ${MAX_FILES} файлов в одном сообщении`);
    for (const f of files.slice(0, Math.max(0, room))) {
      const key = Math.random().toString(36).slice(2);
      const isImg = f.type.startsWith("image/") && f.type !== "image/svg+xml";
      setAtts((as) => [...as, { key, name: f.name, size: f.size, progress: 0, status: "uploading",
        preview: isImg ? URL.createObjectURL(f) : undefined }]);
      const { blob, name } = await prepareImage(f);
      if (blob.size > MAX_MB * 1024 * 1024) {
        patchAtt(key, { status: "error", error: `больше ${MAX_MB} МБ` });
        continue;
      }
      const up = uploadFile(chat.id, blob, name, (p) => patchAtt(key, { progress: p }));
      patchAtt(key, { abort: up.abort, size: blob.size, name });
      up.promise
        .then((file) => patchAtt(key, { status: "done", progress: 1, file }))
        .catch((e) => patchAtt(key, { status: "error", error: e.message }));
    }
  }

  function removeAtt(a: Att) {
    if (a.status === "uploading") a.abort?.();
    if (a.file) api(`/chats/${chat.id}/uploads/${a.file.id}`, { method: "DELETE" }).catch(() => {});
    if (a.preview) URL.revokeObjectURL(a.preview);
    setAtts((as) => as.filter((x) => x.key !== a.key));
  }

  const attsRef = useRef(atts);
  attsRef.current = atts;
  useEffect(() => () => {
    // смена диалога: незагруженные вложения отменяем, превью освобождаем
    attsRef.current.forEach((a) => {
      if (a.status === "uploading") a.abort?.();
      if (a.preview) URL.revokeObjectURL(a.preview);
    });
    setAtts([]);
  }, [chat.id]);

  useEffect(() => {
    seqRef.current = 0;
    const off = socket.on((msg) => {
      if (msg.chat_id !== chat.id) return;
      if (msg.t === "events") {
        if (msg.initial) setLoaded(true);
        const fresh = (msg.events as Ev[]).filter((e) => e.seq > seqRef.current);
        if (!fresh.length) return;
        seqRef.current = Math.max(...fresh.map((e) => e.seq));
        setEvents((es) => (msg.initial && es.length === 0 ? fresh : [...es, ...fresh]));
        if (fresh.some((e) => e.k === "claude" && (e.d?.type === "assistant" || e.d?.type === "result"))) setLive("");
      } else if (msg.t === "live") {
        const ev = msg.d;
        if (ev?.type === "message_start") setLive("");
        else if (ev?.type === "content_block_delta" && ev.delta?.type === "text_delta") setLive((s) => s + ev.delta.text);
      }
    });
    socket.subscribe(chat.id, () => seqRef.current);
    // запасной путь, если сокет ещё не подключён
    const t = setTimeout(() => {
      if (!socket.connected)
        api<{ events: Ev[] }>(`/chats/${chat.id}/events`).then((r) => {
          if (seqRef.current === 0 && r.events.length) {
            seqRef.current = r.events[r.events.length - 1].seq;
            setEvents(r.events);
          }
          setLoaded(true);
        }).catch(() => setLoaded(true));
    }, 1500);
    return () => {
      off();
      clearTimeout(t);
      socket.subscribe(null, () => 0);
    };
  }, [chat.id]);

  const items = useMemo(() => buildItems(events), [events]);

  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [items, live]);

  function onScroll() {
    const el = scroller.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  }

  const uploading = atts.some((a) => a.status === "uploading");
  const ready = atts.filter((a) => a.status === "done" && a.file);
  const canSend = (!!draft.trim() || ready.length > 0) && !uploading && !sending;

  async function send() {
    const text = draft.trim();
    if (!canSend) return;
    setSending(true);
    stick.current = true;
    const files = ready.map((a) => a.file!.id);
    const ok = await runWithStart(() => api(`/chats/${chat.id}/messages`, { method: "POST", body: { text, files } }));
    if (ok) {
      setDraft("");
      atts.forEach((a) => a.preview && URL.revokeObjectURL(a.preview));
      setAtts([]);
    }
    setSending(false);
    taRef.current?.focus();
  }

  async function act(path: string, method = "POST", confirmText?: string) {
    if (confirmText && !confirm(confirmText)) return;
    try {
      await api(`/chats/${chat.id}${path}`, { method });
    } catch (e: any) {
      onToast(e.message);
    }
  }

  const suspended = chat.state === "suspended";
  const pending = new Map(chat.approvals.map((a) => [a.id, a]));

  return (
    <div
      className={"chat" + (drag ? " dragging" : "")}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes("Files")) {
          e.preventDefault();
          setDrag(true);
        }
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node)) setDrag(false);
      }}
      onDrop={(e) => {
        if (!e.dataTransfer.files.length) return;
        e.preventDefault();
        setDrag(false);
        addFiles(e.dataTransfer.files);
      }}
    >
      {drag && <div className="drop-hint">Отпустите, чтобы прикрепить файлы</div>}
      <div className="messages" ref={scroller} onScroll={onScroll}>
        {!loaded && <div className="muted center pad">Загрузка истории…</div>}
        {loaded && items.length === 0 && (
          <div className="muted center pad">Напишите первое сообщение — его первая строка станет названием диалога.</div>
        )}
        {items.map((it) => (
          <ItemView key={it.key} it={it} pending={pending} chatId={chat.id} onToast={onToast} />
        ))}
        {live && (
          <div className="msg assistant">
            <Markdown text={live} />
            <span className="cursor" />
          </div>
        )}
        {chat.busy && !live && chat.approvals.length === 0 && <div className="typing"><span /><span /><span /></div>}
      </div>

      {suspended && (
        <div className="banner">
          <strong>Сессия выгружена из RAM</strong> — история и контекст сохранены на диске в сжатом виде. Отправьте сообщение или нажмите ▶ у диалога в списке: сервер проверит, хватает ли памяти.
          {chat.notice && <div className="small muted">{chat.notice}</div>}
        </div>
      )}

      <div className={"composer" + (tall ? " tall" : "")}>
        {atts.length > 0 && (
          <div className="att-strip">
            {atts.map((a) => (
              <div key={a.key} className={"att " + a.status} title={a.error ? `${a.name}: ${a.error}` : a.name}>
                {a.preview ? <img src={a.preview} alt="" /> : <span className="att-icon">{fileIcon(a.name)}</span>}
                <div className="att-info">
                  <div className="att-name">{a.name}</div>
                  <div className="att-meta">
                    {a.status === "uploading" ? `${Math.round(a.progress * 100)}%` : a.status === "error" ? a.error : fmtSize(a.size)}
                  </div>
                </div>
                {a.status === "uploading" && <div className="att-bar" style={{ width: `${a.progress * 100}%` }} />}
                <button className="att-x" onClick={() => removeAtt(a)} aria-label="Убрать">×</button>
              </div>
            ))}
          </div>
        )}
        <textarea
          ref={taRef}
          rows={tall ? undefined : Math.min(12, Math.max(3, draft.split("\n").length))}
          placeholder={chat.busy ? "Claude отвечает…" : finePointer ? "Сообщение (Enter — отправить, Shift+Enter — новая строка). Файлы можно перетащить или вставить" : "Сообщение"}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onPaste={(e) => {
            const fs = Array.from(e.clipboardData.files);
            if (fs.length) {
              e.preventDefault();
              addFiles(fs);
            }
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && finePointer) {
              e.preventDefault();
              send();
            }
          }}
        />
        <div className="composer-bar">
          <button className="icon-btn attach" title="Прикрепить фото или документ" aria-label="Прикрепить файл"
            onClick={() => fileRef.current?.click()} disabled={atts.length >= MAX_FILES}>📎</button>
          <input ref={fileRef} type="file" multiple hidden onChange={(e) => {
            if (e.target.files) addFiles(e.target.files);
            e.target.value = "";
          }} />
          <button className="icon-btn" onClick={toggleTall} title={tall ? "Уменьшить поле" : "Развернуть поле"}
            aria-label={tall ? "Уменьшить поле" : "Развернуть поле"}>{tall ? "⤡" : "⤢"}</button>
          <span className="spacer" />
          {chat.busy ? (
            <button className="round-btn stop" onClick={() => act("/interrupt")} title="Остановить ответ" aria-label="Остановить ответ">■</button>
          ) : (
            <button className="round-btn send" disabled={!canSend} onClick={send} aria-label="Отправить"
              title={uploading ? "Дождитесь загрузки файлов" : "Отправить"}>{sending ? "…" : "↑"}</button>
          )}
        </div>
      </div>
    </div>
  );
}

const ItemView = memo(function ItemView({ it, pending, chatId, onToast }: {
  it: Item; pending: Map<string, Approval>; chatId: string; onToast: (s: string) => void;
}) {
  switch (it.kind) {
    case "user":
      return (
        <div className="msg user">
          {it.files && it.files.length > 0 && <Attachments chatId={chatId} files={it.files} />}
          {it.text && <div className="user-text">{it.text}</div>}
          <div className="meta">{fmtTime(it.ts)}</div>
        </div>
      );
    case "text":
      return (
        <div className="msg assistant">
          <Markdown text={it.text} />
        </div>
      );
    case "thinking":
      return (
        <details className="thinking">
          <summary>Размышления</summary>
          <div className="pre">{it.text}</div>
        </details>
      );
    case "tool":
      return <ToolCard it={it} />;
    case "result": {
      const d = it.d;
      const err = d.is_error || (d.subtype && d.subtype !== "success");
      return (
        <div className={"turn-end" + (err ? " err" : "")}>
          {err ? `Ход завершён с ошибкой: ${d.subtype}${d.result ? " — " + String(d.result).slice(0, 300) : ""}` : "Готово"}
          {typeof d.duration_ms === "number" && ` · ${(d.duration_ms / 1000).toFixed(1)} с`}
          {typeof d.total_cost_usd === "number" && ` · $${d.total_cost_usd.toFixed(4)}`}
        </div>
      );
    }
    case "notice":
      return (
        <div className={"notice " + it.level}>
          <Markdown text={it.text} />
          <div className="meta">{fmtTime(it.ts)}</div>
        </div>
      );
    case "approval": {
      const live = pending.get(it.a.id);
      return (
        <div className={"approval" + (live ? " live" : "")}>
          <div className="approval-head">⚠ Требуется подтверждение: {it.a.reason}</div>
          <div className="small muted">Инструмент: {it.a.tool}</div>
          <pre className="pre">{summarizeInput(it.a.tool, it.a.input, 2000)}</pre>
          {live ? (
            <div className="row gap">
              <button className="danger-solid" onClick={() => decide(chatId, it.a.id, true, onToast)}>Разрешить</button>
              <button onClick={() => decide(chatId, it.a.id, false, onToast)}>Запретить</button>
            </div>
          ) : (
            <div className={"small " + (it.outcome?.decision === "allow" ? "ok" : "muted")}>
              {it.outcome ? (it.outcome.decision === "allow" ? "✓ Разрешено" : "✕ " + (it.outcome.reason || "Запрещено")) : "Запрос больше не активен"}
            </div>
          )}
        </div>
      );
    }
  }
});

async function decide(chatId: string, id: string, allow: boolean, onToast: (s: string) => void) {
  try {
    await api(`/chats/${chatId}/approvals/${id}`, { method: "POST", body: { allow } });
  } catch (e: any) {
    onToast(e.message);
  }
}

function ToolCard({ it }: { it: Item & { kind: "tool" } }) {
  const res = it.result;
  const status = !res ? "running" : res.is_error ? "error" : "done";
  return (
    <details className={"tool " + status}>
      <summary>
        <span className="tool-name">{it.name}</span>
        <span className="tool-sum">{summarizeInput(it.name, it.input, 120).split("\n")[0]}</span>
        <span className="tool-status">{status === "running" ? "…" : status === "error" ? "ошибка" : "✓"}</span>
      </summary>
      <div className="tool-body">
        <div className="small muted">Вход</div>
        <pre className="pre">{summarizeInput(it.name, it.input, 6000)}</pre>
        {res && (
          <>
            <div className="small muted">Результат</div>
            <pre className={"pre" + (res.is_error ? " err" : "")}>{resultText(res.content).slice(0, 8000)}</pre>
          </>
        )}
      </div>
    </details>
  );
}

function summarizeInput(name: string, input: any, max: number): string {
  if (!input) return "";
  let s: string;
  if (name === "Bash" && input.command) s = input.command;
  else if (input.file_path && (name === "Read" || name === "Write" || name === "Edit")) {
    s = input.file_path;
    if (name === "Edit" && max > 200) s += `\n\n- ${input.old_string ?? ""}\n+ ${input.new_string ?? ""}`;
    if (name === "Write" && max > 200) s += `\n\n${input.content ?? ""}`;
  } else if (input.pattern) s = input.pattern + (input.path ? `  в ${input.path}` : "");
  else if (input.url) s = input.url;
  else if (input.query) s = input.query;
  else if (input.description) s = input.description;
  else s = JSON.stringify(input, null, 2);
  return s.length > max ? s.slice(0, max) + "…" : s;
}

function resultText(c: any): string {
  if (typeof c === "string") return c;
  if (Array.isArray(c)) return c.map((b) => (b.type === "text" ? b.text : `[${b.type}]`)).join("\n");
  return JSON.stringify(c, null, 2);
}

const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{ a: ({ node, ...p }) => <a {...p} target="_blank" rel="noopener noreferrer nofollow" /> }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
});

function fileIcon(name: string) {
  const ext = name.split(".").pop()?.toLowerCase() || "";
  if (ext === "pdf") return "📕";
  if (["doc", "docx", "odt", "rtf"].includes(ext)) return "📝";
  if (["xls", "xlsx", "ods", "csv", "tsv"].includes(ext)) return "📊";
  if (["zip", "gz", "tgz", "7z", "rar", "tar"].includes(ext)) return "🗜";
  if (["png", "jpg", "jpeg", "gif", "webp", "heic"].includes(ext)) return "🖼";
  return "📄";
}

function Attachments({ chatId, files }: { chatId: string; files: UploadedFile[] }) {
  return (
    <div className="msg-files">
      {files.map((f) =>
        f.image ? (
          <a key={f.id} href={uploadUrl(chatId, f.id)} target="_blank" rel="noopener" className="msg-img" title={f.name}>
            <img src={uploadUrl(chatId, f.id)} alt={f.name} loading="lazy" />
          </a>
        ) : (
          <a key={f.id} href={uploadUrl(chatId, f.id)} target="_blank" rel="noopener" className="msg-file" title={f.name}>
            <span className="att-icon">{fileIcon(f.name)}</span>
            <span className="att-info">
              <span className="att-name">{f.name}</span>
              <span className="att-meta">{fmtSize(f.size)}</span>
            </span>
          </a>
        ),
      )}
    </div>
  );
}
