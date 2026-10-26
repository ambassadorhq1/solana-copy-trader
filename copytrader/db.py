"""Tiny DB layer: Postgres via DATABASE_URL, else SQLite (local). SQL uses '?' placeholders."""
import json, sqlite3, threading
from .util import log

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS credits (day TEXT, purpose TEXT, used BIGINT DEFAULT 0, PRIMARY KEY (day, purpose));
CREATE TABLE IF NOT EXISTS tokens (mint TEXT PRIMARY KEY, symbol TEXT, pair_created_at BIGINT,
  creator TEXT, first_slot BIGINT, scouted_at BIGINT);
CREATE TABLE IF NOT EXISTS wallets (address TEXT PRIMARY KEY, status TEXT, score {F} DEFAULT 0, stats TEXT,
  reason TEXT, source TEXT, funder TEXT, discovered_at BIGINT, scored_at BIGINT, backfilled_to BIGINT,
  last_sig TEXT);
CREATE TABLE IF NOT EXISTS trades (sig TEXT, wallet TEXT, mint TEXT, side TEXT, sol {F}, tokens {F},
  decimals INTEGER, pre_tokens {F}, ts BIGINT, slot BIGINT, PRIMARY KEY (sig, wallet, mint));
CREATE INDEX IF NOT EXISTS trades_wallet ON trades (wallet, ts);
CREATE INDEX IF NOT EXISTS trades_mint_slot ON trades (mint, slot);
CREATE TABLE IF NOT EXISTS positions (id {AUTO}, mint TEXT, symbol TEXT, wallet TEXT, opened_ts BIGINT,
  sol_in {F}, tokens_init {F}, tokens_left {F}, decimals INTEGER, entry_price {F}, wallet_price {F},
  exit_mode TEXT DEFAULT 'default', tp1_done INTEGER DEFAULT 0, status TEXT DEFAULT 'open',
  closed_ts BIGINT, sol_out {F} DEFAULT 0, max_mult {F} DEFAULT 1, last_price {F}, tp_ladder TEXT);
CREATE TABLE IF NOT EXISTS price_samples (mint TEXT, ts BIGINT, price {F});
CREATE INDEX IF NOT EXISTS price_samples_mint ON price_samples (mint, ts);
CREATE TABLE IF NOT EXISTS fills (id {AUTO}, position_id INTEGER, ts BIGINT, side TEXT, sol {F},
  tokens {F}, pnl {F} DEFAULT 0, reason TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS events (id {AUTO}, ts BIGINT, kind TEXT, msg TEXT);
"""


class DB:
    def __init__(self, url=None):
        self.pg = bool(url) and url.startswith(("postgres://", "postgresql://"))
        self.url = url
        self._lock = threading.Lock()
        self._connect()
        f, auto = ("DOUBLE PRECISION", "SERIAL PRIMARY KEY") if self.pg else ("REAL", "INTEGER PRIMARY KEY AUTOINCREMENT")
        for stmt in SCHEMA.replace("{F}", f).replace("{AUTO}", auto).split(";"):
            if stmt.strip():
                self.x(stmt)
        self._migrate()

    def _migrate(self):
        """Columns added after v1 shipped (existing databases)."""
        cols = ("SELECT column_name AS c FROM information_schema.columns WHERE table_name='positions'" if self.pg
                else "SELECT name AS c FROM pragma_table_info('positions')")
        have = {r["c"] for r in self.all(cols)}
        if "tp_ladder" not in have:
            self.x("ALTER TABLE positions ADD COLUMN tp_ladder TEXT")

    def _connect(self):
        if self.pg:
            import psycopg2, psycopg2.extras
            self.conn = psycopg2.connect(self.url, cursor_factory=psycopg2.extras.RealDictCursor)
            self.conn.autocommit = True
        else:
            path = (self.url or "sqlite:///copytrader.db").replace("sqlite:///", "") or "copytrader.db"
            self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")

    def x(self, sql, params=(), _retry=True):
        if self.pg:
            sql = sql.replace("?", "%s")
        with self._lock:
            try:
                cur = self.conn.cursor()
                cur.execute(sql, tuple(params))
                return cur
            except Exception as e:  # reconnect once (free Postgres drops idle connections)
                if self.pg and _retry and ("closed" in str(e).lower() or "terminat" in str(e).lower()):
                    log.warning("db reconnect: %s", e)
                    self._connect()
                else:
                    raise
        return self.x(sql, params, _retry=False)

    def all(self, sql, params=()):
        return [dict(r) for r in self.x(sql, params).fetchall()]

    def one(self, sql, params=()):
        r = self.x(sql, params).fetchone()
        return dict(r) if r else None

    def val(self, sql, params=(), default=None):
        r = self.one(sql, params)
        v = next(iter(r.values())) if r else None
        return default if v is None else v

    def insert(self, sql, params=()):
        """INSERT ... RETURNING id -> id (works on SQLite >= 3.35 and Postgres)."""
        r = self.x(sql + " RETURNING id", params).fetchone()
        return dict(r)["id"] if r else None

    def kv_get(self, k, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return json.loads(r["v"]) if r else default

    def kv_set(self, k, v):
        self.x("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT (k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))

    def event(self, kind, msg):
        from .util import now
        self.x("INSERT INTO events (ts, kind, msg) VALUES (?, ?, ?)", (now(), kind, msg[:500]))
