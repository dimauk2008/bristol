import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.environ.get('DB_PATH') or (Path(__file__).parent / 'coupons.db'))


def _pg_conninfo():
    """Строка подключения PostgreSQL. None — работаем на SQLite."""
    for key in ('DATABASE_URL', 'POSTGRES_URL', 'POSTGRESQL_URL', 'DB_URL'):
        value = (os.environ.get(key) or '').strip()
        if value.startswith(('postgres://', 'postgresql://')):
            return value.replace('postgres://', 'postgresql://', 1)
    if os.environ.get('DB_BACKEND', '').lower() == 'postgres':
        return ''
    if os.environ.get('PGHOST') or os.environ.get('PGDATABASE'):
        return ''
    return None


PG_CONNINFO = _pg_conninfo()
IS_PG = PG_CONNINFO is not None


class _PgCursor:
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=()):
        self._cur.execute(sql.replace('?', '%s'), params or ())
        return self

    def executemany(self, sql, params):
        self._cur.executemany(sql.replace('?', '%s'), params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall() or []

    def close(self):
        self._cur.close()

    @property
    def rowcount(self):
        return self._cur.rowcount


class _PgConn:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _PgCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()


def get_conn():
    if IS_PG:
        import psycopg
        from psycopg.rows import dict_row
        return _PgConn(psycopg.connect(PG_CONNINFO, row_factory=dict_row))
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _cols(cur, table):
    try:
        if IS_PG:
            cur.execute(
                'SELECT column_name FROM information_schema.columns '
                'WHERE table_schema=current_schema() AND table_name=?',
                (table,),
            )
            return {r['column_name'] for r in cur.fetchall()}
        return {r[1] for r in cur.execute(f'PRAGMA table_info({table})').fetchall()}
    except Exception:
        return set()


def _pk():
    return 'SERIAL PRIMARY KEY' if IS_PG else 'INTEGER PRIMARY KEY AUTOINCREMENT'


def _now_default():
    return '(CURRENT_TIMESTAMP::text)' if IS_PG else 'CURRENT_TIMESTAMP'


def _migrate_meta(cur):
    """bristol_meta: одиночная -> по аккаунтам (PK (account_id, key))."""
    cols = _cols(cur, 'bristol_meta')
    if not cols or 'account_id' in cols:
        return
    cur.execute('DROP TABLE IF EXISTS bristol_meta_new')
    cur.execute('''CREATE TABLE bristol_meta_new (
        account_id INTEGER NOT NULL DEFAULT 1,
        key TEXT NOT NULL,
        value TEXT,
        PRIMARY KEY (account_id, key)
    )''')
    cur.execute(
        'INSERT INTO bristol_meta_new (account_id, key, value) '
        'SELECT 1, key, value FROM bristol_meta'
    )
    cur.execute('DROP TABLE bristol_meta')
    cur.execute('ALTER TABLE bristol_meta_new RENAME TO bristol_meta')


def _migrate_coupons(cur):
    """bristol_coupons: одиночная -> по аккаунтам (PK (account_id, number))."""
    cols = _cols(cur, 'bristol_coupons')
    if not cols or 'account_id' in cols:
        return
    disc = 'discount' if 'discount' in cols else 'NULL'
    title = 'title' if 'title' in cols else 'NULL'
    cur.execute('DROP TABLE IF EXISTS bristol_coupons_new')
    cur.execute('''CREATE TABLE bristol_coupons_new (
        account_id INTEGER NOT NULL DEFAULT 1,
        number TEXT NOT NULL,
        universal_coupon TEXT,
        coupon_type TEXT,
        image_url TEXT,
        actual_start TEXT,
        actual_end TEXT,
        synced_at TEXT,
        discount INTEGER,
        title TEXT,
        PRIMARY KEY (account_id, number)
    )''')
    cur.execute(
        f'''INSERT INTO bristol_coupons_new
        (account_id, number, universal_coupon, coupon_type, image_url,
         actual_start, actual_end, synced_at, discount, title)
        SELECT 1, number, universal_coupon, coupon_type, image_url,
               actual_start, actual_end, synced_at, {disc}, {title}
        FROM bristol_coupons'''
    )
    cur.execute('DROP TABLE bristol_coupons')
    cur.execute('ALTER TABLE bristol_coupons_new RENAME TO bristol_coupons')


def _add_col(cur, table, col, decl):
    if col not in _cols(cur, table):
        cur.execute(f'ALTER TABLE {table} ADD COLUMN {col} {decl}')


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f'''CREATE TABLE IF NOT EXISTS accounts (
        id {_pk()},
        name TEXT,
        phone TEXT,
        card_id TEXT,
        external_id TEXT,
        refresh_token TEXT,
        token TEXT,
        created_at TEXT DEFAULT {_now_default()}
    )''')
    cur.execute(f'''CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        name TEXT,
        card_data TEXT,
        active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT {_now_default()},
        expires_at TEXT,
        account_id INTEGER DEFAULT 1
    )''')
    cur.execute(f'''CREATE TABLE IF NOT EXISTS coupons (
        id {_pk()},
        code TEXT UNIQUE NOT NULL,
        value INTEGER DEFAULT 0,
        status TEXT DEFAULT 'available',
        note TEXT,
        used_at TEXT,
        used_by_session_id TEXT,
        created_at TEXT DEFAULT {_now_default()}
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS bristol_meta (
        account_id INTEGER NOT NULL DEFAULT 1,
        key TEXT NOT NULL,
        value TEXT,
        PRIMARY KEY (account_id, key)
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS bristol_coupons (
        account_id INTEGER NOT NULL DEFAULT 1,
        number TEXT NOT NULL,
        universal_coupon TEXT,
        coupon_type TEXT,
        image_url TEXT,
        actual_start TEXT,
        actual_end TEXT,
        synced_at TEXT,
        discount INTEGER,
        title TEXT,
        PRIMARY KEY (account_id, number)
    )''')
    _blob = 'BYTEA' if IS_PG else 'BLOB'
    cur.execute(f'''CREATE TABLE IF NOT EXISTS bristol_images (
        number TEXT PRIMARY KEY,
        content {_blob} NOT NULL,
        content_type TEXT,
        updated_at TEXT
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS bristol_purchases (
        account_id INTEGER NOT NULL DEFAULT 1,
        purchase_id TEXT NOT NULL,
        type TEXT,
        number TEXT,
        purchase_date TEXT,
        total REAL,
        total_text TEXT,
        shop_name TEXT,
        items_count TEXT,
        products TEXT,
        fiscal TEXT,
        raw TEXT,
        synced_at TEXT,
        PRIMARY KEY (account_id, purchase_id)
    )''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_bristol_purchases_acc_date '
                'ON bristol_purchases (account_id, purchase_date DESC)')
    cur.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    cur.execute(f'''CREATE TABLE IF NOT EXISTS coupon_grants (
        token TEXT PRIMARY KEY,
        number TEXT NOT NULL,
        active INTEGER DEFAULT 1,
        expires_at TEXT,
        created_at TEXT DEFAULT {_now_default()},
        account_id INTEGER DEFAULT 1
    )''')

    _add_col(cur, 'sessions', 'expires_at', 'TEXT')
    _add_col(cur, 'sessions', 'account_id', 'INTEGER DEFAULT 1')
    _add_col(cur, 'sessions', 'show_coupons', 'INTEGER DEFAULT 1')
    _add_col(cur, 'coupon_grants', 'account_id', 'INTEGER DEFAULT 1')
    _migrate_meta(cur)
    _migrate_coupons(cur)

    cur.execute('SELECT COUNT(*) AS n FROM accounts')
    if (cur.fetchone()['n'] or 0) == 0:
        cur.execute(
            "INSERT INTO accounts (id, name, phone) VALUES (1, ?, ?)",
            ('Аккаунт 1', ''),
        )
        if IS_PG:
            cur.execute(
                "SELECT setval(pg_get_serial_sequence('accounts', 'id'), "
                "GREATEST((SELECT COALESCE(MAX(id), 0) FROM accounts), 1))"
            )
        cur.execute(
            "INSERT INTO settings (key, value) VALUES ('active_account_id', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )

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


def get_active_account_id() -> int:
    try:
        return int(get_setting('active_account_id', '1'))
    except (TypeError, ValueError):
        return 1


def set_active_account_id(account_id: int) -> None:
    set_setting('active_account_id', str(int(account_id)))
