-- Схема применяется при каждом старте, все операции идемпотентны.
CREATE TABLE IF NOT EXISTS users (
    id               SERIAL PRIMARY KEY,
    username         TEXT UNIQUE NOT NULL,
    password_hash    TEXT NOT NULL,
    is_admin         BOOLEAN NOT NULL DEFAULT FALSE,
    rights           TEXT[] NOT NULL DEFAULT '{}',
    max_sessions     INT NOT NULL DEFAULT 2,
    allowed_projects TEXT[] NOT NULL DEFAULT '{}',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login       TIMESTAMPTZ
);
-- Админ может быть только один (самый первый пользователь).
CREATE UNIQUE INDEX IF NOT EXISTS users_single_admin ON users (is_admin) WHERE is_admin;

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    user_agent  TEXT,
    ip          TEXT
);
CREATE INDEX IF NOT EXISTS auth_sessions_user ON auth_sessions (user_id);

CREATE TABLE IF NOT EXISTS invites (
    code_hash   TEXT PRIMARY KEY,
    created_by  INT REFERENCES users(id) ON DELETE CASCADE,
    note        TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    used_by     INT REFERENCES users(id) ON DELETE SET NULL,
    used_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS chats (
    id                 UUID PRIMARY KEY,
    user_id            INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title              TEXT NOT NULL DEFAULT 'Новый диалог',
    project            TEXT,
    model              TEXT,
    claude_session_id  UUID NOT NULL,
    state              TEXT NOT NULL DEFAULT 'suspended',   -- running | suspended
    has_transcript     BOOLEAN NOT NULL DEFAULT FALSE,
    notice             TEXT,                                 -- последнее системное сообщение (почему выгружен и т.п.)
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_activity      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chats_user ON chats (user_id);
-- все session_id, которые Claude выдавал этому чату (при --resume id меняется)
ALTER TABLE chats ADD COLUMN IF NOT EXISTS past_session_ids UUID[] NOT NULL DEFAULT '{}';

-- Журнал событий: выгрузки, экстренные завершения, удаления админом, сбои.
CREATE TABLE IF NOT EXISTS incidents (
    id          SERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL,
    user_id     INT REFERENCES users(id) ON DELETE CASCADE,
    chat_id     UUID,
    chat_title  TEXT,
    message     TEXT NOT NULL,
    details     JSONB NOT NULL DEFAULT '{}',
    seen        BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS incidents_user ON incidents (user_id, ts DESC);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  JSONB NOT NULL
);

-- Клиентские сертификаты устройств (mTLS). Ключи на сервере не хранятся.
CREATE TABLE IF NOT EXISTS client_certs (
    id           SERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    fingerprint  TEXT UNIQUE NOT NULL,          -- sha256(DER), hex
    serial       TEXT NOT NULL,
    user_id      INT REFERENCES users(id) ON DELETE SET NULL,   -- привязывается при первом входе
    created_by   INT REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    last_used    TIMESTAMPTZ,
    last_ip      TEXT
);
-- сессия входа привязана к сертификату устройства, с которого выполнен вход
ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS cert_fp TEXT;
