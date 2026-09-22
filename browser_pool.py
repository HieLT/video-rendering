"""Browser Account Pool: Manages accounts/ profiles with concurrency control and daily limits."""
import asyncio
from contextlib import asynccontextmanager
import shutil
import sqlite3
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from dola_client import CreditError
from video_worker_ui import (
    AccountLimitedError, CreditInsufficientError, RiskControlError, generate_video, resume_video,
)
import config


def _log(message: str):
    """Keeps pool diagnostics printable on Windows cp1252 consoles."""
    print(str(message).encode("ascii", "backslashreplace").decode("ascii"), flush=True)

DAILY_LIMIT = 2
COOLDOWN_SEC = 1800  # 30-minute cooldown on risk control


class AllAccountsLimitedError(RuntimeError):
    """All active schedulable accounts have reached daily video limit."""


class AllAccountsQuotaBlockedError(RuntimeError):
    """All active schedulable accounts are known to have insufficient credits."""


class BrowserPool:
    def __init__(self, accounts_dir: str = "accounts", db_path: str = "pool_usage.db",
                 max_concurrency: int = 1):
        self.accounts_dir = Path(accounts_dir)
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self._locks: dict[str, asyncio.Lock] = {}
        self._activities: dict[str, str] = {}
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS usage (account TEXT, day TEXT, used INTEGER, "
            "PRIMARY KEY(account, day))"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts_meta (
                name TEXT PRIMARY KEY,
                scheduling INTEGER DEFAULT 1,
                note TEXT DEFAULT '',
                email TEXT DEFAULT '',
                created_at REAL,
                last_used_at REAL DEFAULT 0,
                login_ok INTEGER,
                login_checked_at REAL DEFAULT 0,
                cooldown_until REAL DEFAULT 0,
                rate_limited_until REAL DEFAULT 0,
                limit_reason TEXT DEFAULT '',
                quota_blocked_until REAL DEFAULT 0,
                quota_reason TEXT DEFAULT '',
                credit_balance INTEGER,
                credit_checked_at REAL DEFAULT 0
            )
            """
        )
        self._conn.commit()
        # Legacy migration: add metadata columns
        for column, definition in (
            ("auth_state", "TEXT DEFAULT 'unverified'"),
            ("auth_error", "TEXT DEFAULT ''"),
            ("dispatch_manual", "INTEGER DEFAULT 0"),
            ("email", "TEXT DEFAULT ''"),
            ("account_type", "TEXT DEFAULT 'unknown'"),
            ("display_name", "TEXT DEFAULT ''"),
            ("identity_hash", "TEXT DEFAULT ''"),
            ("dola_user_id", "TEXT DEFAULT ''"),
            ("rate_limited_until", "REAL DEFAULT 0"),
            ("limit_reason", "TEXT DEFAULT ''"),
            ("quota_blocked_until", "REAL DEFAULT 0"),
            ("quota_reason", "TEXT DEFAULT ''"),
            ("credit_balance", "INTEGER"),
            ("credit_checked_at", "REAL DEFAULT 0"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE accounts_meta ADD COLUMN {column} {definition}")
                if column == "dispatch_manual":
                    self._conn.execute("UPDATE accounts_meta SET dispatch_manual=1 WHERE scheduling=0")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

    @asynccontextmanager
    async def account_activity(self, account: str, activity: str):
        lock = self._locks.setdefault(account, asyncio.Lock())
        async with lock:
            self._activities[account] = activity
            try:
                yield
            finally:
                self._activities.pop(account, None)

    # ===== Account Discovery & Metadata =====

    def _ensure_meta(self, name: str):
        self._conn.execute(
            "INSERT OR IGNORE INTO accounts_meta (name, created_at) VALUES (?, ?)",
            (name, time.time()),
        )
        self._conn.commit()

    @property
    def accounts(self) -> list:
        if not self.accounts_dir.exists():
            return []
        names = sorted(d.name for d in self.accounts_dir.iterdir()
                       if d.is_dir() and not d.name.startswith("."))
        for n in names:
            self._ensure_meta(n)
        return names

    def _meta(self, name: str):
        return self._conn.execute(
            "SELECT * FROM accounts_meta WHERE name=?", (name,)).fetchone()

    def used_today(self, account: str) -> int:
        row = self._conn.execute(
            "SELECT used FROM usage WHERE account=? AND day=?",
            (account, date.today().isoformat()),
        ).fetchone()
        return row[0] if row else 0

    def _claim(self, account: str):
        self._conn.execute(
            "INSERT INTO usage(account, day, used) VALUES (?,?,1) "
            "ON CONFLICT(account, day) DO UPDATE SET used=used+1",
            (account, date.today().isoformat()),
        )
        self._conn.commit()

    def _next_limit_reset(self) -> float:
        """Calculates next daily quota reset timestamp."""
        try:
            tz = ZoneInfo(config.LIMIT_RESET_TZ)
        except Exception:
            # Fallback to fixed offset if tzdata is not installed.
            offsets = {"Asia/Tokyo": 9, "Asia/Hong_Kong": 8, "UTC": 0}
            tz = timezone(timedelta(hours=offsets.get(config.LIMIT_RESET_TZ, 9)))
        now = datetime.now(tz)
        next_day = now.date() + timedelta(days=1)
        return datetime.combine(next_day, dt_time.min, tzinfo=tz).timestamp()

    def _clear_expired_rate_limits(self):
        now = time.time()
        cur = self._conn.execute(
            "UPDATE accounts_meta SET rate_limited_until=0, limit_reason='', "
            "quota_blocked_until=0, quota_reason='' "
            "WHERE (rate_limited_until > 0 AND rate_limited_until <= ?) "
            "OR (quota_blocked_until > 0 AND quota_blocked_until <= ?)", (now, now))
        if cur.rowcount:
            self._conn.commit()

    def _mark_quota_blocked(self, account: str, reason: str = ""):
        self._conn.execute(
            "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=?, last_used_at=? WHERE name=?",
            (self._next_limit_reset(), reason[:300], time.time(), account),
        )
        self._conn.commit()

    def _mark_daily_limit(self, account: str, reason: str = ""):
        """Marks account as reaching daily limit until next reset."""
        self._conn.execute(
            "INSERT INTO usage(account, day, used) VALUES (?,?,?) "
            "ON CONFLICT(account, day) DO UPDATE SET used=MAX(used, excluded.used)",
            (account, date.today().isoformat(), DAILY_LIMIT),
        )
        self._conn.execute(
            "UPDATE accounts_meta SET last_used_at=?, rate_limited_until=?, limit_reason=? WHERE name=?",
            (time.time(), self._next_limit_reset(), reason[:300], account),
        )
        self._conn.commit()

    def list_accounts(self) -> list:
        """Dashboard view: combines metadata, quota, and busy status."""
        self._clear_expired_rate_limits()
        now = time.time()
        out = []
        for a in self.accounts:
            m = self._meta(a)
            used = self.used_today(a)
            lock = self._locks.get(a)
            out.append({
                "name": a,
                "auth_state": m["auth_state"],
                "auth_error": m["auth_error"],
                "account_type": m["account_type"] if m else "unknown",
                "display_name": m["display_name"] if m else "",
                "scheduling": bool(m["scheduling"]) if m else True,
                "note": m["note"] if m else "",
                "email": m["email"] if m else "",
                "created_at": m["created_at"] if m else 0,
                "last_used_at": m["last_used_at"] if m else 0,
                "login_ok": m["login_ok"] if m else None,
                "login_checked_at": m["login_checked_at"] if m else 0,
                "cooldown_until": m["cooldown_until"] if m else 0,
                "cooling": bool(m and m["cooldown_until"] > now),
                "rate_limited_until": m["rate_limited_until"] if m and m["rate_limited_until"] else 0,
                "rate_limited": bool(m and m["rate_limited_until"] > now),
                "limit_reason": m["limit_reason"] if m else "",
                "quota_blocked_until": m["quota_blocked_until"] if m and m["quota_blocked_until"] else 0,
                "quota_blocked": bool(m and m["quota_blocked_until"] > now),
                "quota_reason": m["quota_reason"] if m else "",
                "credit_balance": m["credit_balance"] if m else None,
                "credit_checked_at": m["credit_checked_at"] if m else 0,
                "used_today": used,
                "limit": DAILY_LIMIT,
                "remaining": max(0, DAILY_LIMIT - used),
                "busy": bool(lock and lock.locked()),
                "activity": self._activities.get(a) if lock and lock.locked() else None,
            })
        return out

    def set_scheduling(self, name: str, on: bool, manual: bool = True):
        self._conn.execute(
            "UPDATE accounts_meta SET scheduling=?, dispatch_manual=? WHERE name=?", (1 if on else 0, int(manual), name))
        self._conn.commit()

    def set_email(self, name: str, email: str):
        self._conn.execute(
            "UPDATE accounts_meta SET email=? WHERE name=?", (email, name))
        self._conn.commit()

    def set_login_status(self, name: str, ok: bool):
        self._conn.execute(
            "UPDATE accounts_meta SET login_ok=?, login_checked_at=? WHERE name=?",
            (1 if ok else 0, time.time(), name),
        )
        self._conn.commit()

    def set_note(self, name: str, note: str):
        self._conn.execute(
            "UPDATE accounts_meta SET note=? WHERE name=?", (note, name))
        self._conn.commit()

    def delete_account(self, name: str):
        lock = self._locks.get(name)
        if lock and lock.locked():
            raise RuntimeError("Account is generating video, cannot delete")
        root = self.accounts_dir.resolve()
        d = (root / name).resolve()
        if d.parent != root or d == root:
            raise RuntimeError("Invalid account profile path")
        _log(f"[account-delete] account={name!r} stage=remove_profile path={str(d)!r} exists={d.exists()}")
        try:
            if d.exists():
                shutil.rmtree(d)
        except OSError as e:
            _log(f"[account-delete] account={name!r} stage=remove_profile_failed "
                 f"type={type(e).__name__} file={e.filename!r} errno={e.errno} "
                 f"winerror={getattr(e, 'winerror', None)} error={str(e)!r}")
            raise
        _log(f"[account-delete] account={name!r} stage=remove_metadata")
        self._conn.execute("DELETE FROM accounts_meta WHERE name=?", (name,))
        self._conn.commit()
        _log(f"[account-delete] account={name!r} stage=metadata_removed")

    def auth_result(self, name, state, error="", identity=None):
        identity = identity or {}
        if state == "active":
            key = identity.get("identity_hash", "")
            uid = identity.get("dola_user_id", "")
            if not key and not uid:
                raise RuntimeError("Verified account identity missing")
            duplicate = self._conn.execute(
                "SELECT name FROM accounts_meta WHERE name<>? AND auth_state<>'duplicate' "
                "AND ((?<>'' AND dola_user_id=?) OR (?<>'' AND identity_hash=?))",
                (name, uid, uid, key, key)).fetchone()
            if duplicate:
                state, error = "duplicate", f"Duplicate account: {duplicate['name']}"
        meta = self._meta(name)
        scheduling = meta["scheduling"]
        login_ok = meta["login_ok"]
        if state == "active":
            login_ok = 1
            if not meta["dispatch_manual"]:
                scheduling = 1
        elif state in ("expired", "duplicate", "failed", "adding"):
            login_ok, scheduling = 0, 0
        self._conn.execute(
            "UPDATE accounts_meta SET auth_state=?, auth_error=?, login_ok=?, scheduling=?, login_checked_at=?, "
            "dola_user_id=CASE WHEN ?<>'' THEN ? ELSE dola_user_id END, "
            "identity_hash=CASE WHEN ?<>'' THEN ? ELSE identity_hash END, "
            "display_name=CASE WHEN display_name='' THEN ? ELSE display_name END WHERE name=?",
            (state, error, login_ok, scheduling, time.time(),
             identity.get("dola_user_id", ""), identity.get("dola_user_id", ""), identity.get("identity_hash", ""),
             identity.get("identity_hash", ""), identity.get("display_name", ""), name))
        self._conn.commit()
        return state

    async def verify_account(self, name: str) -> bool:
        if name not in self.accounts:
            raise FileNotFoundError("Account does not exist")
        lock = self._locks.setdefault(name, asyncio.Lock())
        if lock.locked():
            raise RuntimeError("Account is busy")
        from browser import inspect_account_session
        async with self.account_activity(name, "verifying"):
            try:
                result = await inspect_account_session(name)
                state = self.auth_result(name, result["state"], result.get("error", ""), result)
            except Exception as exc:
                self.auth_result(name, "check_error", "Could not check session; retry when connection is available")
                raise RuntimeError("Could not check session; account was not marked expired") from exc
        if state == "duplicate":
            raise RuntimeError(self._meta(name)["auth_error"])
        return state == "active"

    # ===== Scheduling =====

    def _set_credit_balance(self, account: str, balance: int, source: str = ""):
        self._conn.execute(
            "UPDATE accounts_meta SET credit_balance=?, credit_checked_at=? WHERE name=?",
            (max(0, int(balance)), time.time(), account),
        )
        if balance < 2:
            self._conn.execute(
                "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=? WHERE name=?",
                (self._next_limit_reset(), source[:300] or "Insufficient credits", account),
            )
        self._conn.commit()

    def _credit_available(self, account: str, required: int = 2) -> bool:
        row = self._meta(account)
        return not row or row["credit_balance"] is None or row["credit_balance"] >= required

    def _schedulable(self, a: dict) -> bool:
        return (a["scheduling"] and a.get("login_ok") == 1 and a.get("auth_state") == "active" and not a["cooling"] and not a["rate_limited"]
                and not a["quota_blocked"] and a["used_today"] < DAILY_LIMIT
                and (a["credit_balance"] is None or a["credit_balance"] >= 2))

    @property
    def all_accounts_limited(self) -> bool:
        """Returns True if all active accounts have reached daily limit."""
        candidates = [a for a in self.list_accounts() if a["scheduling"] and not a["cooling"]]
        return bool(candidates) and all(
            a["rate_limited"] or a["used_today"] >= DAILY_LIMIT for a in candidates
        )

    @property
    def all_accounts_quota_blocked(self) -> bool:
        candidates = [a for a in self.list_accounts() if a["scheduling"] and not a["cooling"]]
        return bool(candidates) and all(
            a["quota_blocked"] or a["rate_limited"] or a["used_today"] >= DAILY_LIMIT
            for a in candidates
        ) and any(a["quota_blocked"] for a in candidates)

    @property
    def available(self) -> bool:
        return any(self._schedulable(a) for a in self.list_accounts())

    @property
    def cookie_count(self) -> int:  # /health compatibility
        return len(self.accounts)

    def account_status(self) -> list:
        return [{
            "account": a["name"], "used_today": a["used_today"], "limit": a["limit"],
            "rate_limited": a["rate_limited"], "rate_limited_until": a["rate_limited_until"],
            "quota_blocked": a["quota_blocked"], "quota_blocked_until": a["quota_blocked_until"],
        } for a in self.list_accounts()]

    async def resume_video(self, account: str, conversation_id: str, timeout: int,
                           on_poll=None) -> dict:
        """Resumes an accepted session without re-scheduling."""
        async with self.semaphore:
            lock = self._locks.setdefault(account, asyncio.Lock())
            async with self.account_activity(account, "generating"):
                def on_balance(balance, source=""):
                    self._set_credit_balance(account, balance, source)
                try:
                    result = await resume_video(account, conversation_id, timeout,
                                                on_poll=on_poll, on_balance=on_balance)
                    self._claim(account)
                    self._conn.execute(
                        "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                        (time.time(), account))
                    self._conn.commit()
                    return result
                except TimeoutError:
                    self._claim(account)
                    self._conn.commit()
                    raise

    async def generate_video(self, prompt: str, ratio: str = None, duration: int = None,
                             model: str = "seedance_v2.0", on_conversation_id=None,
                             on_poll=None, on_balance=None,
                             reference_image_paths: list[str] | None = None) -> dict:
        """Picks an idle schedulable account; automatically rotates on quota/risk limits."""
        async with self.semaphore:
            last_err = None
            for a in self.list_accounts():
                if not self._schedulable(a):
                    continue
                account = a["name"]
                _log(f"[pool] selected account={account} for generation")
                lock = self._locks.setdefault(account, asyncio.Lock())
                # Skip busy accounts to prevent concurrent collisions on same profile.
                if lock.locked():
                    continue
                async with self.account_activity(account, "generating"):
                    if not self._schedulable(next(x for x in self.list_accounts() if x['name'] == account)):
                        continue  # State changed while waiting
                    submit_attempted = False

                    def on_submit():
                        nonlocal submit_attempted
                        submit_attempted = True

                    try:
                        def on_balance(balance, source=""):
                            self._set_credit_balance(account, balance, source)

                        result = await generate_video(
                            account, prompt, ratio, duration, model=model,
                            on_conversation_id=on_conversation_id, on_poll=on_poll,
                            on_balance=on_balance, reference_image_paths=reference_image_paths,
                            on_submit=on_submit)
                        self._claim(account)
                        self._conn.execute(
                            "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                            (time.time(), account))
                        self._conn.commit()
                        return result
                    except CreditInsufficientError as e:
                        _log(f"[pool] {account} insufficient points before generation, skipping: {e}")
                        self._mark_quota_blocked(account, str(e))
                        last_err = e
                        continue
                    except AccountLimitedError as e:
                        _log(f"[pool] {account} reached daily limit; current Playwright context is closed by worker, rotating: {e}")
                        self._mark_daily_limit(account, str(e))
                        last_err = e
                        continue
                    except CreditError as e:
                        _log(f"[pool] {account} out of quota, rotating: {e}")
                        self._claim(account)
                        last_err = e
                        continue
                    except RiskControlError as e:
                        _log(f"[pool] {account} risk control triggered (30m cooldown), rotating: {e}")
                        self._conn.execute(
                            "UPDATE accounts_meta SET cooldown_until=? WHERE name=?",
                            (time.time() + COOLDOWN_SEC, account))
                        self._conn.commit()
                        last_err = e
                        continue
                    except TimeoutError as e:
                        # Setup failures consume no generation. After an Enter
                        # attempt, conservatively count it and never auto-resubmit.
                        if submit_attempted:
                            self._claim(account)
                            self._conn.execute(
                                "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                        raise
                    except FileNotFoundError as e:
                        _log(f"[pool] {account} profile missing, skipping: {e}")
                        last_err = e
                        continue
            if self.all_accounts_quota_blocked:
                raise AllAccountsQuotaBlockedError(
                    f"429: All schedulable accounts have insufficient points: {last_err or 'No accounts'}"
                )
            if self.all_accounts_limited:
                raise AllAccountsLimitedError(
                    f"429: All schedulable accounts have reached Dola daily limit: {last_err or 'No accounts'}"
                )
            raise RuntimeError(f"No available accounts in pool: {last_err or 'No accounts'}")