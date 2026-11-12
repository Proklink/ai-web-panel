// Одно WebSocket-соединение на вкладку, с автоматическим переподключением.
type Listener = (msg: any) => void;

class Socket {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private sub: { chat_id: string; after: () => number } | null = null;
  private retry = 0;
  private pingTimer: number | undefined;
  private closedByUs = false;
  connected = false;
  onStatus: (ok: boolean) => void = () => {};

  start() {
    this.closedByUs = false;
    if (this.ws) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/ws`);
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.connected = true;
      this.onStatus(true);
      if (this.sub) this.send({ op: "subscribe", chat_id: this.sub.chat_id, after: this.sub.after() });
      this.pingTimer = window.setInterval(() => this.send({ op: "ping" }), 25000);
    };
    ws.onmessage = (e) => {
      let msg: any;
      try {
        msg = JSON.parse(e.data);
      } catch {
        return;
      }
      this.listeners.forEach((l) => l(msg));
    };
    ws.onclose = (e) => {
      this.ws = null;
      this.connected = false;
      this.onStatus(false);
      window.clearInterval(this.pingTimer);
      if (e.code === 4401) {
        window.dispatchEvent(new Event("wp-unauthorized"));
        return;
      }
      if (!this.closedByUs) {
        const delay = Math.min(15000, 500 * 2 ** this.retry++);
        window.setTimeout(() => this.start(), delay);
      }
    };
  }

  stop() {
    this.closedByUs = true;
    this.ws?.close();
    this.ws = null;
  }

  send(obj: any) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  subscribe(chat_id: string | null, after: () => number) {
    this.sub = chat_id ? { chat_id, after } : null;
    if (chat_id) this.send({ op: "subscribe", chat_id, after: after() });
    else this.send({ op: "unsubscribe" });
  }

  on(l: Listener) {
    this.listeners.add(l);
    return () => {
      this.listeners.delete(l);
    };
  }
}

export const socket = new Socket();
