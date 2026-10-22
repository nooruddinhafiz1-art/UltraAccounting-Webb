-- UltraAccounting Database Setup (Supabase PostgreSQL)
-- Ye script Supabase SQL Editor me chalayein

CREATE TABLE IF NOT EXISTS shops (
    id TEXT PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    owner TEXT,
    mobile TEXT,
    email TEXT,
    address TEXT,
    gst_no TEXT,
    logo TEXT,
    bill_prefix TEXT DEFAULT 'INV',
    print_size TEXT DEFAULT 'A4',
    plan TEXT DEFAULT 'trial',
    active_until BIGINT,
    created_at BIGINT,
    active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    shop_id TEXT NOT NULL,
    username TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    name TEXT,
    active INTEGER DEFAULT 1,
    created_at BIGINT,
    perms TEXT,
    UNIQUE(shop_id, username)
);

-- Purane database ke liye (agar users table pehle se bani hai):
ALTER TABLE users ADD COLUMN IF NOT EXISTS perms TEXT;

CREATE TABLE IF NOT EXISTS records (
    shop_id TEXT NOT NULL,
    store TEXT NOT NULL,
    id TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at BIGINT NOT NULL,
    PRIMARY KEY (shop_id, store, id)
);

CREATE INDEX IF NOT EXISTS records_updated_idx ON records (shop_id, updated_at);

CREATE TABLE IF NOT EXISTS super_admin (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at BIGINT
);

-- Default super admin (password: ultra@2026) - login ke baad change kar lena
-- Hash generate karna hai to app.py se kar lena
-- INSERT INTO super_admin (id, username, password_hash, created_at) VALUES ('sa-1', 'superadmin', '...', 0);