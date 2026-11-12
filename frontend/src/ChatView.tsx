import { memo, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, Approval, Chat, Ev, fmtMb, fmtTime } from "./api";
import { socket } from "./ws";
import type { RunWithStart } from "./App";

type Item =
  | { kind: "user"; key: string; text: string; ts: number }
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
    if (ev.k === "user") items.push({ kind: "user", key: "u" + ev.seq, text: d.text, ts: ev.ts });
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

  async function send() {
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    stick.current = true;
    const ok = await runWithStart(() => api(`/chats/${chat.id}/messages`, { method: "POST", body: { text } }));
    if (ok) setDraft("");
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
    <div className="chat">
      <div className="chat-head">
        <div className="chat-head-title">
          <strong title={chat.title}>{chat.title}</strong>
          <span className="muted small">
            {chat.project ? `проект ${chat.project}` : "без проекта"}
            {chat.model ? ` · ${chat.model}` : ""}
            {" · "}
            {suspended ? "выгружен на диск" : chat.busy ? "отвечает…" : "в RAM"}
            {!suspended && chat.rss_mb > 0 && ` · ${fmtMb(chat.rss_mb)}`}
          </span>
        </div>
        <div className="row gap">
          {suspended ? (
            <button onClick={() => runWithStart(() => api(`/chats/${chat.id}/resume`, { method: "POST" }))}>Вернуть в RAM</button>
          ) : (
            <button onClick={() => act("/suspend")} disabled={chat.busy} title="Остановить процесс, сжать и сохранить контекст на диск">
              Выгрузить
            </button>
          )}
          <button className="danger" onClick={() => act("", "DELETE", "Удалить диалог безвозвратно? Будут стёрты история, контекст Claude и файлы личной папки диалога.")}>
            Удалить
          </button>
        </div>
      </div>

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
          <strong>Сессия выгружена из RAM</strong> — история и контекст сохранены на диске в сжатом виде. Отправьте сообщение или нажмите «Вернуть в RAM»: сервер проверит, хватает ли памяти.
          {chat.notice && <div className="small muted">{chat.notice}</div>}
        </div>
      )}

      <div className="composer">
        <textarea
          ref={taRef}
          rows={Math.min(8, Math.max(1, draft.split("\n").length))}
          placeholder={chat.busy ? "Claude отвечает…" : finePointer ? "Сообщение (Enter — отправить, Shift+Enter — новая строка)" : "Сообщение"}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && finePointer) {
              e.preventDefault();
              send();
            }
          }}
        />
        {chat.busy ? (
          <button className="danger" onClick={() => act("/interrupt")}>Стоп</button>
        ) : (
          <button className="primary" disabled={!draft.trim() || sending} onClick={send}>
            {sending ? "…" : "Отправить"}
          </button>
        )}
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
          <div className="user-text">{it.text}</div>
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
