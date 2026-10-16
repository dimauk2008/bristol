import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get('DB_PATH') or (Path(__file__).parent / 'coupons.db'))

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        name TEXT,
        card_data TEXT,
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS coupons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        value INTEGER DEFAULT 0,
        status TEXT DEFAULT 'available',
        note TEXT,
        used_at TEXT,
        used_by_session_id TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS bristol_meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS bristol_coupons (
        number TEXT PRIMARY KEY,
        universal_coupon TEXT,
        coupon_type TEXT,
        image_url TEXT,
        actual_start TEXT,
        actual_end TEXT,
        synced_at TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS coupon_grants (
        token TEXT PRIMARY KEY,
        number TEXT NOT NULL,
        active INTEGER DEFAULT 1,
        expires_at TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )''')
    cols = {r[1] for r in cur.execute('PRAGMA table_info(sessions)').fetchall()}
    if 'expires_at' not in cols:
        cur.execute('ALTER TABLE sessions ADD COLUMN expires_at TEXT')
    bcols = {r[1] for r in cur.execute('PRAGMA table_info(bristol_coupons)').fetchall()}
    if 'discount' not in bcols:
        cur.execute('ALTER TABLE bristol_coupons ADD COLUMN discount INTEGER')
    if 'title' not in bcols:
        cur.execute('ALTER TABLE bristol_coupons ADD COLUMN title TEXT')
    conn.commit()
    conn.close()


def get_setting(key: str, default: str = '') -> str:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT value FROM settings WHERE key=?', (key,))
    row = cur.fetchone()
    conn.close()
    return row['value'] if row else default


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO settings (key, value) VALUES (?,?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, value),
    )
    conn.commit()
    conn.close()