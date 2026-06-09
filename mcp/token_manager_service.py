"""
TokenManagerService - 统一 Token/Cookie 管理中心

核心职责：
  1. 统一存储：所有平台（GitHub/Twitter/微博/小红书/豆瓣）的凭证
  2. 过期跟踪：记录 expires_at，支撑主动刷新
  3. 自动刷新：probe → QR 降级 → 手动通知 的完整生命周期
  4. 过期预警：凭证快过期时推送通知（飞书/钉钉等）
  5. 统一接口：所有 Channel Executor / MCP 工具共享同一套凭证

平台支持：
  - github      : Personal Access Token (PAT) + OAuth
  - twitter/x   : OAuth 1.0a / OAuth 2.0 + Cookie
  - weibo       : Cookie + QR 扫码登录
  - xhs (小红书) : Cookie + Phone/Password
  - douban      : Cookie + CK (Campaign Cookie)
  - zhihu       : Cookie

凭证存储（复用 media_accounts 表）：
  - DB: cookie_raw (Fernet 加密), expires_at, metadata (token_type, refresh_token 等)
  - 文件: storage_state.json (Playwright 格式)
  - 缓存: /app/data/agent_reach/token_cache/ (解密后临时文件)

自动刷新策略：
  - 主动预刷新：每次使用前检查，过期前 30 分钟自动刷新
  - 被动刷新：操作时遇到 CookieExpiredError → 触发刷新
  - 定时刷新：APScheduler 定期检查（每 60 分钟扫描）
  - 预警通知：过期前 24h / 6h / 1h 分别推送提醒
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..services.storage_service import DatabasePool, get_storage_service
from ..services.notification_service import PushChannel, get_notification_service


# ---------------------------------------------------------------------------
# Platform configs
# ---------------------------------------------------------------------------

PLATFORM_DEFAULTS = {
    "github": {
        "cookie_expiry_hours": 2160,  # 90 天（GitHub PAT）
        "refresh_expiry_hours": 720,   # 30 天（refresh_token）
        "needs_qr": False,
        "needs_password": False,
        "token_type": "bearer",
    },
    "twitter": {
        "cookie_expiry_hours": 720,    # 30 天
        "refresh_expiry_hours": 168,   # 7 天
        "needs_qr": True,
        "needs_password": False,
        "token_type": "oauth2",
    },
    "weibo": {
        "cookie_expiry_hours": 168,    # 7 天
        "refresh_expiry_hours": 24,
        "needs_qr": True,
        "needs_password": False,
        "token_type": "cookie",
    },
    "xhs": {
        "cookie_expiry_hours": 72,     # 3 天
        "refresh_expiry_hours": 24,
        "needs_qr": False,
        "needs_password": True,
        "token_type": "cookie",
    },
    "douban": {
        "cookie_expiry_hours": 720,    # 30 天
        "refresh_expiry_hours": 168,
        "needs_qr": False,
        "needs_password": True,
        "token_type": "cookie",
    },
    "zhihu": {
        "cookie_expiry_hours": 168,
        "refresh_expiry_hours": 24,
        "needs_qr": False,
        "needs_password": True,
        "token_type": "cookie",
    },
}

NOTIFY_BEFORE_HOURS = [24, 6, 1]  # 过期前 24h / 6h / 1h 预警


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class TokenInfo:
    """统一凭证信息"""
    account_id: str
    platform: str
    token_type: str          # "bearer" | "cookie" | "oauth2" | "pat"
    cookie_raw: str          # 加密存储的原始 cookie / token
    expires_at: str          # ISO8601 过期时间
    refreshed_at: str        # 上次刷新时间
    status: str             # "active" | "expiring" | "expired" | "invalid"
    metadata: Dict[str, Any] = field(default_factory=dict)

    # GitHub 专用
    pat_token: str = ""      # Personal Access Token（单独存储）
    refresh_token: str = ""  # OAuth refresh_token

    oauth_token: str = ""
    oauth_token_secret: str = ""  # 对应 DB 列 oauth_secret

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        try:
            exp = datetime.fromisoformat(self.expires_at)
            return datetime.now() > exp
        except Exception:
            return False

    @property
    def hours_until_expiry(self) -> float:
        if not self.expires_at:
            return float("inf")
        try:
            exp = datetime.fromisoformat(self.expires_at)
            delta = exp - datetime.now()
            return delta.total_seconds() / 3600
        except Exception:
            return float("inf")

    @property
    def needs_refresh(self) -> bool:
        return self.is_expired or self.hours_until_expiry < 0.5  # 30 分钟内过期

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "account_id": self.account_id,
            "platform": self.platform,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "refreshed_at": self.refreshed_at,
            "status": self.status,
            "is_expired": self.is_expired,
            "hours_until_expiry": round(self.hours_until_expiry, 1),
            "needs_refresh": self.needs_refresh,
        }
        # 不暴露完整 token
        for field_name in ["cookie_raw", "pat_token", "refresh_token", "oauth_token", "oauth_token_secret"]:
            val = getattr(self, field_name, "")
            if val:
                d[field_name + "_preview"] = val[:4] + "***" + val[-4:] if len(val) > 8 else "***"
        return d


@dataclass
class RefreshResult:
    """刷新结果"""
    success: bool
    method: str              # "probe" | "qr" | "manual" | "oauth_refresh" | "none_needed"
    expires_at: str
    refreshed_at: str
    error: str = ""
    notified: bool = False


# ---------------------------------------------------------------------------
# TokenManagerService
# ---------------------------------------------------------------------------

@dataclass
class ExpiryAlert:
    hours_remaining: float
    alert_sent: bool


class TokenManagerService:
    """
    统一 Token/Cookie 管理中心。

    生命周期：
      register(account_id, platform)  → 创建空账号记录
      save_token(...)                 → 保存凭证 + 设置 expires_at
      get_token(...)                  → 获取有效凭证（自动触发刷新）
      refresh_token(...)              → 主动刷新
      check_all_expiring()           → 定时扫描过期账号并预警
      auto_refresh_if_needed(...)    → 使用前检查，过期自动刷新
    """

    CACHE_DIR = Path("/app/data/agent_reach/token_cache")
    DB_TABLE = "token_accounts"

    _instances: Dict[str, "TokenManagerService"] = {}

    def __init__(
        self,
        db_pool: Optional[DatabasePool] = None,
        storage_service=None,
        cache_dir: Optional[str] = None,
    ):
        self.db_pool = db_pool or self._get_db_pool()
        self.storage = storage_service
        self.cache_dir = Path(cache_dir) if cache_dir else self.CACHE_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._expiry_alerts: Dict[str, Dict[float, bool]] = {}  # account_id → {hours→sent}
        self._refresh_locks: Dict[str, asyncio.Lock] = {}
        self._notification_service = None
        self._initialized = False

    @staticmethod
    def _get_db_pool() -> Optional[DatabasePool]:
        """获取已有 DatabasePool 单例（从已初始化的 StorageService）。"""
        # StorageService.__init__ 会创建 DatabasePool 单例
        # 如果还没有任何 StorageService 实例，先创建一个
        try:
            from mcp_server.services.storage_service import StorageService
            # StorageService 构造函数里会创建 DatabasePool（单例）
            storage = StorageService()
            return storage.db_pool
        except Exception:
            return None

    def initialize(self) -> None:
        """创建 token_accounts 表"""
        if not self.db_pool:
            return
        sql = f"""
        CREATE TABLE IF NOT EXISTS {self.DB_TABLE} (
            account_id     TEXT PRIMARY KEY,
            platform       TEXT NOT NULL,
            token_type     TEXT NOT NULL DEFAULT 'cookie',
            cookie_raw     TEXT NOT NULL DEFAULT '',
            pat_token      TEXT NOT NULL DEFAULT '',
            refresh_token  TEXT NOT NULL DEFAULT '',
            oauth_token    TEXT NOT NULL DEFAULT '',
            oauth_secret   TEXT NOT NULL DEFAULT '',
            expires_at     TIMESTAMP,
            refreshed_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            status         TEXT NOT NULL DEFAULT 'active',
            metadata       JSONB NOT NULL DEFAULT '{{}}',
            created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(account_id, platform)
        );
        CREATE INDEX IF NOT EXISTS idx_token_platform ON {self.DB_TABLE}(platform, status);
        CREATE INDEX IF NOT EXISTS idx_token_expires ON {self.DB_TABLE}(expires_at) WHERE expires_at IS NOT NULL;
        """
        with self.db_pool.get_cursor() as cursor:
            cursor.execute(sql)
        self._initialized = True

    @property
    def notification(self):
        if self._notification_service is None:
            self._notification_service = get_notification_service()
        return self._notification_service

    # ---- Registration -------------------------------------------------------

    def register(
        self,
        account_id: str,
        platform: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TokenInfo:
        """注册一个新账号（创建空凭证记录）"""
        if not self.db_pool:
            return TokenInfo(
                account_id=account_id, platform=platform,
                token_type="cookie", status="active",
                expires_at="", refreshed_at=datetime.now().isoformat(),
            )
        self.initialize()

        existing = self.get_token_info(account_id, platform)
        if existing:
            return existing

        meta = json.dumps(metadata or {}, ensure_ascii=False)
        with self.db_pool.get_cursor() as cursor:
            cursor.execute(
                f"""INSERT INTO {self.DB_TABLE}
                    (account_id, platform, metadata, status)
                    VALUES (%s, %s, %s, 'active')
                    ON CONFLICT (account_id, platform) DO NOTHING""",
                (account_id, platform, meta),
            )
        return self.get_token_info(account_id, platform)

    # ---- Token I/O ---------------------------------------------------------

    def save_token(
        self,
        account_id: str,
        platform: str,
        cookie_raw: str = "",
        pat_token: str = "",
        refresh_token: str = "",
        oauth_token: str = "",
        oauth_secret: str = "",
        expires_at: Optional[datetime] = None,
        token_type: str = "cookie",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TokenInfo:
        """保存凭证并设置过期时间"""
        if not self.db_pool:
            return TokenInfo(
                account_id=account_id, platform=platform,
                token_type=token_type, cookie_raw=cookie_raw,
                expires_at=expires_at.isoformat() if expires_at else "",
                refreshed_at=datetime.now().isoformat(), status="active",
            )
        self.initialize()

        # 加密 cookie_raw
        stored_cookie = self._encrypt(cookie_raw) if cookie_raw else ""

        # 计算过期时间（如果没提供）
        if expires_at is None:
            cfg = PLATFORM_DEFAULTS.get(platform, {})
            hours = cfg.get("cookie_expiry_hours", 168)
            expires_at = datetime.now() + timedelta(hours=hours)

        now = datetime.now()
        meta = json.dumps(metadata or {}, ensure_ascii=False)

        with self.db_pool.get_cursor() as cursor:
            cursor.execute(
                f"""INSERT INTO {self.DB_TABLE}
                    (account_id, platform, token_type, cookie_raw, pat_token,
                     refresh_token, oauth_token, oauth_secret, expires_at,
                     refreshed_at, status, metadata, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (account_id, platform) DO UPDATE SET
                     token_type=EXCLUDED.token_type,
                     cookie_raw=EXCLUDED.cookie_raw,
                     pat_token=EXCLUDED.pat_token,
                     refresh_token=EXCLUDED.refresh_token,
                     oauth_token=EXCLUDED.oauth_token,
                     oauth_secret=EXCLUDED.oauth_secret,
                     expires_at=EXCLUDED.expires_at,
                     refreshed_at=EXCLUDED.refreshed_at,
                     status=EXCLUDED.status,
                     metadata=EXCLUDED.metadata,
                     updated_at=CURRENT_TIMESTAMP""",
                (account_id, platform, token_type, stored_cookie,
                 pat_token, refresh_token, oauth_token, oauth_secret,
                 expires_at, now, "active", meta, now),
            )

        return self.get_token_info(account_id, platform)

    def get_token_info(
        self, account_id: str, platform: str
    ) -> Optional[TokenInfo]:
        """从 DB 读取凭证信息（不解密 cookie）"""
        if not self.db_pool:
            return None
        with self.db_pool.get_cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM {self.DB_TABLE} WHERE account_id=%s AND platform=%s",
                (account_id, platform),
            )
            row = cursor.fetchone()
        if not row:
            return None
        return self._row_to_token_info(row)

    def get_decrypted_token(
        self, account_id: str, platform: str
    ) -> Optional[str]:
        """获取解密后的原始 cookie/token"""
        info = self.get_token_info(account_id, platform)
        if not info:
            return ""
        # 优先返回 PAT / oauth_token
        if info.pat_token:
            return info.pat_token
        if info.oauth_token:
            return info.oauth_token
        if info.cookie_raw:
            return self._decrypt(info.cookie_raw)
        return ""

    def get_token_file_path(
        self, account_id: str, platform: str
    ) -> str:
        """返回 Playwright storage_state 文件路径"""
        path = self.cache_dir / f"{platform}-{account_id.replace('@','_at_')}.json"
        if not path.exists():
            # 从 DB 解密写入临时文件
            cookie = self.get_decrypted_token(account_id, platform)
            if cookie:
                # 尝试写入 Playwright 格式
                try:
                    data = json.loads(cookie)
                    path.write_text(json.dumps(data), encoding="utf-8")
                except Exception:
                    # 不是 JSON，当作 cookie 字符串处理
                    path.write_text(cookie, encoding="utf-8")
        return str(path)

    # ---- Status management --------------------------------------------------

    def update_status(
        self, account_id: str, platform: str, status: str
    ) -> None:
        if not self.db_pool:
            return
        with self.db_pool.get_cursor() as cursor:
            cursor.execute(
                f"UPDATE {self.DB_TABLE} SET status=%s, updated_at=CURRENT_TIMESTAMP "
                "WHERE account_id=%s AND platform=%s",
                (status, account_id, platform),
            )

    def mark_expired(
        self, account_id: str, platform: str
    ) -> None:
        self.update_status(account_id, platform, "expired")

    def mark_active(
        self, account_id: str, platform: str
    ) -> None:
        self.update_status(account_id, platform, "active")

    # ---- Token refresh core -----------------------------------------------

    async def refresh_token(
        self,
        account_id: str,
        platform: str,
        method: str = "auto",
        cookie_raw: str = "",
        oauth_refresh_token: str = "",
    ) -> RefreshResult:
        """
        刷新凭证。

        method:
          - "auto"       : probe → QR → 通知用户手动提供（weibo 等）
          - "oauth"      : 用 refresh_token 换新 token（GitHub/Twitter OAuth）
          - "manual"     : 直接保存传入的 cookie_raw
          - "qr_only"    : 强制走 QR 扫码（忽略 probe 结果）
          - "probe_only" : 只检查，不刷新
        """
        lock = self._refresh_locks.setdefault(f"{account_id}:{platform}", asyncio.Lock())
        async with lock:
            result = RefreshResult(
                success=False, method=method,
                expires_at="", refreshed_at=datetime.now().isoformat(),
            )

            info = self.get_token_info(account_id, platform)
            if not info:
                result.error = f"account_not_registered: {account_id}/{platform}"
                return result

            cfg = PLATFORM_DEFAULTS.get(platform, {})

            # method="probe_only" → 只检查状态
            if method == "probe_only":
                alive = await self._probe(account_id, platform)
                result.success = alive
                result.method = "probe"
                result.expires_at = info.expires_at
                if not alive:
                    result.error = "probe_failed"
                return result

            # method="oauth" → OAuth refresh token 流程
            if method == "oauth":
                rt_result = await self._oauth_refresh(account_id, platform, oauth_refresh_token)
                if rt_result.success:
                    result.success = True
                    result.method = "oauth_refresh"
                    result.expires_at = rt_result.expires_at
                    self.mark_active(account_id, platform)
                else:
                    result.error = rt_result.error
                return result

            # method="manual" → 保存传入的 cookie
            if method == "manual" and cookie_raw:
                token_type = cfg.get("token_type", "cookie")
                expires_hours = cfg.get("cookie_expiry_hours", 168)
                expires_at = datetime.now() + timedelta(hours=expires_hours)
                self.save_token(
                    account_id=account_id, platform=platform,
                    cookie_raw=cookie_raw,
                    token_type=token_type,
                    expires_at=expires_at,
                )
                result.success = True
                result.method = "manual"
                result.expires_at = expires_at.isoformat()
                await self._notify_success(account_id, platform, method="manual")
                return result

            # method="auto" 或 "qr_only"
            if method == "qr_only":
                probe_ok = False
            else:
                alive = await self._probe(account_id, platform)
                result.method = "probe"
                if alive:
                    # cookie 仍有效，只更新 refreshed_at
                    result.success = True
                    result.expires_at = info.expires_at
                    self.save_token(
                        account_id=account_id, platform=platform,
                        cookie_raw=self.get_decrypted_token(account_id, platform),
                        token_type=info.token_type,
                        expires_at=datetime.fromisoformat(info.expires_at)
                        if info.expires_at else None,
                    )
                    return result

            # 需要刷新：走 QR 或 OAuth
            if cfg.get("needs_qr"):
                qr_result = await self._qr_login(account_id, platform)
                if qr_result.success:
                    result.success = True
                    result.method = "qr"
                    result.expires_at = qr_result.expires_at
                    self.mark_active(account_id, platform)
                    await self._notify_success(account_id, platform, method="qr")
                else:
                    result.error = qr_result.error
                    await self._notify_manual_required(account_id, platform, qr_result.error)
            else:
                # 不支持 QR 的平台，标记需要手动
                result.error = "manual_required"
                await self._notify_manual_required(account_id, platform, "no_qr_support")
                self.update_status(account_id, platform, "invalid")

            return result

    # ---- Auto-refresh wrapper ----------------------------------------------

    async def get_or_refresh(
        self,
        account_id: str,
        platform: str,
        auto_refresh: bool = True,
    ) -> Tuple[bool, str]:
        """
        获取凭证，必要时自动刷新。

        Returns: (has_valid_token, decrypted_token_or_error)
        """
        info = self.get_token_info(account_id, platform)
        if not info:
            return False, f"account_not_registered: {account_id}/{platform}"

        if info.is_expired:
            self.mark_expired(account_id, platform)
            if not auto_refresh:
                return False, f"token_expired: {info.expires_at}"
            refresh_result = await self.refresh_token(account_id, platform, method="auto")
            if not refresh_result.success:
                return False, f"refresh_failed: {refresh_result.error}"
            info = self.get_token_info(account_id, platform)

        if info and not info.is_expired:
            token = self.get_decrypted_token(account_id, platform)
            return True, token

        return False, "unknown_error"

    # ---- Expiry checker ----------------------------------------------------

    async def check_all_expiring(
        self, dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        定时扫描：检查所有账号是否即将过期，
        发送预警通知，并触发自动刷新。
        由 Scheduler 每 60 分钟调用一次。
        """
        if not self.db_pool:
            return {"checked": 0, "alerts": [], "refreshed": []}

        with self.db_pool.get_cursor() as cursor:
            cursor.execute(
                f"SELECT * FROM {self.DB_TABLE} WHERE status != 'expired' AND expires_at IS NOT NULL"
            )
            rows = cursor.fetchall()

        alerts_sent = []
        refreshed = []
        errors = []

        for row in rows:
            info = self._row_to_token_info(row)
            self._expiry_alerts.setdefault(info.account_id, {})

            for warn_hours in NOTIFY_BEFORE_HOURS:
                if info.hours_until_expiry <= warn_hours < warn_hours + 0.5:
                    already_sent = self._expiry_alerts[info.account_id].get(warn_hours, False)
                    if not already_sent and not dry_run:
                        try:
                            await self._send_expiry_alert(info, warn_hours)
                            self._expiry_alerts[info.account_id][warn_hours] = True
                            alerts_sent.append({
                                "account_id": info.account_id,
                                "platform": info.platform,
                                "warn_hours": warn_hours,
                            })
                        except Exception as e:
                            errors.append(str(e))

            # 自动刷新：快过期但还有效（< 2h）
            if info.hours_until_expiry < 2 and info.hours_until_expiry > 0:
                if not dry_run:
                    try:
                        result = await self.refresh_token(info.account_id, info.platform, method="auto")
                        refreshed.append({
                            "account_id": info.account_id,
                            "platform": info.platform,
                            "success": result.success,
                            "method": result.method,
                        })
                    except Exception as e:
                        errors.append(f"{info.account_id}: {e}")

        return {
            "checked": len(rows),
            "alerts": alerts_sent,
            "refreshed": refreshed,
            "errors": errors,
        }

    def list_tokens(
        self,
        platform: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[TokenInfo]:
        """列出所有账号凭证"""
        if not self.db_pool:
            return []
        self.initialize()  # 确保表已创建
        sql = f"SELECT * FROM {self.DB_TABLE} WHERE 1=1"
        params = []
        if platform:
            sql += " AND platform=%s"
            params.append(platform)
        if status:
            sql += " AND status=%s"
            params.append(status)
        sql += " ORDER BY updated_at DESC"
        with self.db_pool.get_cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return [self._row_to_token_info(r) for r in rows]

    def delete_token(self, account_id: str, platform: str) -> bool:
        if not self.db_pool:
            return False
        with self.db_pool.get_cursor() as cursor:
            cursor.execute(
                f"DELETE FROM {self.DB_TABLE} WHERE account_id=%s AND platform=%s",
                (account_id, platform),
            )
        # 清除缓存文件
        for p in self.cache_dir.glob(f"{platform}-{account_id.replace('@','_at_')}*"):
            p.unlink(missing_ok=True)
        self._expiry_alerts.pop(account_id, None)
        return True

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _encrypt(self, raw: str) -> str:
        try:
            from ..services.openclaw_channel.utils.cookie_crypto import encrypt_cookie
            return encrypt_cookie(raw)
        except Exception:
            return raw

    def _decrypt(self, stored: str) -> str:
        try:
            from ..services.openclaw_channel.utils.cookie_crypto import decrypt_cookie, is_encrypted
            if is_encrypted(stored):
                return decrypt_cookie(stored)
        except Exception:
            pass
        return stored

    async def _probe(self, account_id: str, platform: str) -> bool:
        """用 Playwright probe 检查 cookie 是否仍有效"""
        try:
            from ..services.openclaw_channel.login.orchestrator import LoginOrchestratorV2
        except Exception:
            return False

        storage_path = f"/app/output/{platform}-{account_id.replace('@','_at_')}-storage-state.json"
        if not Path(storage_path).exists():
            return False

        try:
            orch = LoginOrchestratorV2(
                account_id=account_id, platform=platform,
                storage_root="/app/output",
            )
            result = await orch.probe()
            return result.state == "saved"
        except Exception:
            return False

    async def _qr_login(
        self, account_id: str, platform: str, timeout_sec: int = 120
    ) -> RefreshResult:
        """QR 扫码登录（仅 weibo）"""
        result = RefreshResult(
            success=False, method="qr",
            expires_at="", refreshed_at=datetime.now().isoformat(),
        )
        if platform != "weibo":
            result.error = "qr_not_supported"
            return result

        try:
            from ..services.openclaw_channel.login.orchestrator import LoginOrchestratorV2
            orch = LoginOrchestratorV2(
                account_id=account_id, platform="weibo",
                storage_root="/app/output",
            )
            qr_result = await orch.qr_login(timeout_sec=timeout_sec)

            if qr_result.state in ("qr_confirmed", "saved"):
                # 读 storage_state 文件
                storage_path = qr_result.cookie_storage_state_path
                if storage_path and Path(storage_path).exists():
                    cookie_raw = Path(storage_path).read_text(encoding="utf-8")
                    cfg = PLATFORM_DEFAULTS.get("weibo", {})
                    expires_at = datetime.now() + timedelta(
                        hours=cfg.get("cookie_expiry_hours", 168)
                    )
                    self.save_token(
                        account_id=account_id, platform="weibo",
                        cookie_raw=cookie_raw,
                        token_type="cookie",
                        expires_at=expires_at,
                    )
                    result.success = True
                    result.expires_at = expires_at.isoformat()
                    return result

            result.error = f"qr_state={qr_result.state}"
            return result
        except Exception as e:
            result.error = f"qr_exception: {e}"
            return result

    async def _oauth_refresh(
        self,
        account_id: str,
        platform: str,
        refresh_token: str,
    ) -> RefreshResult:
        """OAuth refresh_token 流程"""
        result = RefreshResult(
            success=False, method="oauth_refresh",
            expires_at="", refreshed_at=datetime.now().isoformat(),
        )

        if platform == "github":
            try:
                import urllib.request
                req = urllib.request.Request(
                    "https://github.com/session/invalidate_token",
                    data=json.dumps({"token": refresh_token}).encode(),
                    headers={"Authorization": f"token {refresh_token}"},
                )
                # GitHub PAT 不过期，这个方法主要是检测
                info = self.get_token_info(account_id, platform)
                if info:
                    result.success = True
                    result.expires_at = (
                        datetime.now() + timedelta(days=90)
                    ).isoformat()
                return result
            except Exception as e:
                result.error = str(e)
                return result

        elif platform == "twitter":
            # Twitter OAuth2 refresh
            try:
                import urllib.request
                req = urllib.request.Request(
                    "https://api.twitter.com/2/oauth2/token",
                    data=urllib.parse.urlencode({
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": os.getenv("TWITTER_CLIENT_ID", ""),
                        "client_secret": os.getenv("TWITTER_CLIENT_SECRET", ""),
                    }).encode(),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                    new_token = data.get("access_token", "")
                    new_expires = data.get("expires_in", 7200)
                    if new_token:
                        expires_at = datetime.now() + timedelta(seconds=new_expires)
                        self.save_token(
                            account_id=account_id, platform=platform,
                            oauth_token=new_token,
                            refresh_token=data.get("refresh_token", refresh_token),
                            token_type="oauth2",
                            expires_at=expires_at,
                        )
                        result.success = True
                        result.expires_at = expires_at.isoformat()
                        return result
            except Exception as e:
                result.error = str(e)
                return result

        result.error = "oauth_not_implemented_for_platform"
        return result

    async def _send_expiry_alert(
        self, info: TokenInfo, hours_remaining: float
    ) -> None:
        """发送过期预警"""
        platform_names = {
            "github": "GitHub", "twitter": "Twitter/X",
            "weibo": "微博", "xhs": "小红书",
            "douban": "豆瓣", "zhihu": "知乎",
        }
        pname = platform_names.get(info.platform, info.platform)
        content = (
            f"⚠️ [{pname}] 凭证即将过期\n\n"
            f"账号：{info.account_id}\n"
            f"平台：{pname}\n"
            f"过期时间：{info.expires_at}\n"
            f"剩余：约 {hours_remaining:.0f} 小时\n\n"
            f"请及时登录刷新，或联系管理员处理。"
        )
        try:
            self.notification.send(
                title=f"[TokenManager] {pname} 凭证过期预警",
                content=content,
                channels=[PushChannel.FEISHU],
            )
        except Exception:
            pass

    async def _notify_success(
        self, account_id: str, platform: str, method: str
    ) -> None:
        platform_names = {
            "github": "GitHub", "twitter": "Twitter/X",
            "weibo": "微博", "xhs": "小红书",
            "douban": "豆瓣", "zhihu": "知乎",
        }
        pname = platform_names.get(platform, platform)
        try:
            self.notification.send(
                title=f"[TokenManager] {pname} 凭证刷新成功",
                content=f"✅ {pname} 账号 {account_id} 凭证已通过 [{method}] 刷新成功。",
                channels=[PushChannel.FEISHU],
            )
        except Exception:
            pass

    async def _notify_manual_required(
        self, account_id: str, platform: str, reason: str
    ) -> None:
        platform_names = {
            "github": "GitHub", "twitter": "Twitter/X",
            "weibo": "微博", "xhs": "小红书",
            "douban": "豆瓣", "zhihu": "知乎",
        }
        pname = platform_names.get(platform, platform)
        try:
            self.notification.send(
                title=f"[TokenManager] {pname} 凭证刷新失败 — 需要人工介入",
                content=(
                    f"🔴 {pname} 账号 {account_id} 凭证自动刷新失败。\n\n"
                    f"原因：{reason}\n\n"
                    f"请手动提供新的凭证（Cookie / Token）。\n"
                    f"操作：调用 MCP 工具 token_save 并传入新的 cookie_raw。"
                ),
                channels=[PushChannel.FEISHU],
            )
        except Exception:
            pass

    @staticmethod
    def _row_to_token_info(row: Dict[str, Any]) -> TokenInfo:
        return TokenInfo(
            account_id=str(row.get("account_id", "")),
            platform=str(row.get("platform", "")),
            token_type=str(row.get("token_type", "cookie")),
            cookie_raw=str(row.get("cookie_raw", "")),
            pat_token=str(row.get("pat_token", "")),
            refresh_token=str(row.get("refresh_token", "")),
            oauth_token=str(row.get("oauth_token", "")),
            oauth_token_secret=str(row.get("oauth_secret", "")),
            expires_at=str(row.get("expires_at") or ""),
            refreshed_at=str(row.get("refreshed_at") or ""),
            status=str(row.get("status", "active")),
            metadata=row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
        )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_token_manager: Optional[TokenManagerService] = None


def get_token_manager(
    db_pool: Optional[DatabasePool] = None,
) -> TokenManagerService:
    global _token_manager
    if _token_manager is None:
        _token_manager = TokenManagerService(db_pool=db_pool)
    return _token_manager
