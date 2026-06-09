"""
Token Management MCP Tools

暴露以下工具给外部智能体：
  token_register     注册新账号
  token_save         保存凭证（Cookie/PAT/OAuth）
  token_get          获取有效凭证（自动触发刷新）
  token_status       查询账号状态
  token_list         列出所有账号
  token_refresh      主动刷新凭证
  token_delete       删除账号
  token_check_expiring  定时检查所有过期账号（供 Scheduler 调用）
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from ..services.token_manager import (
    TokenManagerService,
    get_token_manager,
)


class TokenManagerTools:
    """Token 管理 MCP 工具类"""

    def __init__(self, project_root: Optional[str] = None):
        self.project_root = project_root
        self._manager: Optional[TokenManagerService] = None

    @property
    def manager(self) -> TokenManagerService:
        if self._manager is None:
            self._manager = get_token_manager()
        return self._manager

    # ---- Tool: register ---------------------------------------------------

    def token_register(
        self,
        account_id: str,
        platform: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        注册一个新账号到 TokenManager（不提供凭证，仅创建记录）。

        Args:
            account_id: 账号唯一标识（如 "halfking_weibo"、"bot_01_github"）
            platform: 平台（weibo / github / twitter / xhs / douban / zhihu）
            metadata: 附加元数据（phone, email, owner 等）

        Returns:
            JSON 格式的注册结果

        Example:
            token_register(account_id="my_weibo", platform="weibo", metadata={"phone": "138****"})
        """
        try:
            info = self.manager.register(account_id, platform, metadata)
            return json.dumps({
                "success": True,
                "account_id": info.account_id,
                "platform": info.platform,
                "status": info.status,
                "expires_at": info.expires_at,
            }, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    # ---- Tool: save -------------------------------------------------------

    def token_save(
        self,
        account_id: str,
        platform: str,
        cookie_raw: str = "",
        pat_token: str = "",
        refresh_token: str = "",
        oauth_token: str = "",
        expires_in_hours: Optional[int] = None,
        token_type: str = "cookie",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        保存凭证到 TokenManager（加密存储 + 设置过期时间）。

        支持平台及凭证类型：
          - weibo   : cookie_raw（微博 Cookie，建议从浏览器复制）
          - github  : pat_token（Personal Access Token）或 cookie_raw
          - twitter : oauth_token + oauth_token_secret 或 cookie_raw
          - xhs     : cookie_raw
          - douban  : cookie_raw
          - zhihu   : cookie_raw

        Args:
            account_id: 账号唯一标识
            platform: 平台名
            cookie_raw: 原始 Cookie 字符串（JSON 或 key=value 格式）
            pat_token: GitHub PAT（覆盖 cookie_raw）
            refresh_token: OAuth refresh_token
            oauth_token: OAuth access_token
            expires_in_hours: 手动指定过期小时数（默认由平台默认值决定）
            token_type: 凭证类型（cookie / pat / oauth2 / bearer）
            metadata: 附加元数据

        Returns:
            JSON 格式的保存结果（含过期时间）

        Example:
            token_save(account_id="my_weibo", platform="weibo",
                       cookie_raw='{"SRFTOKEN":"xxx","SUB":"_2A":"xxx"}',
                       expires_in_hours=168)
        """
        try:
            from datetime import datetime, timedelta
            expires_at = None
            if expires_in_hours:
                expires_at = datetime.now() + timedelta(hours=expires_in_hours)

            info = self.manager.save_token(
                account_id=account_id,
                platform=platform,
                cookie_raw=cookie_raw,
                pat_token=pat_token,
                refresh_token=refresh_token,
                oauth_token=oauth_token,
                expires_at=expires_at,
                token_type=token_type,
                metadata=metadata,
            )
            return json.dumps({
                "success": True,
                "account_id": info.account_id,
                "platform": info.platform,
                "token_type": info.token_type,
                "expires_at": info.expires_at,
                "refreshed_at": info.refreshed_at,
                "status": info.status,
            }, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    # ---- Tool: get -------------------------------------------------------

    def token_get(
        self,
        account_id: str,
        platform: str,
        auto_refresh: bool = True,
    ) -> str:
        """
        获取有效凭证（自动触发刷新 if expired）。

        优先获取顺序：PAT > OAuth token > Cookie
        过期时自动刷新（auto_refresh=True），失败返回错误。

        Args:
            account_id: 账号唯一标识
            platform: 平台名
            auto_refresh: 过期时是否自动刷新（默认 True）

        Returns:
            JSON 格式的凭证信息（不含完整 token，仅 preview + 文件路径）

        Example:
            token_get(account_id="my_weibo", platform="weibo")
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        has_token, result = loop.run_until_complete(
            self.manager.get_or_refresh(account_id, platform, auto_refresh=auto_refresh)
        )

        info = self.manager.get_token_info(account_id, platform)
        token_file = self.manager.get_token_file_path(account_id, platform) if has_token else ""

        return json.dumps({
            "success": has_token,
            "account_id": account_id,
            "platform": platform,
            "has_valid_token": has_token,
            "token_file_path": token_file,
            "token_preview": (result[:4] + "***" + result[-4:]) if len(result) > 8 and has_token else ("***" if result else ""),
            "expires_at": info.expires_at if info else "",
            "hours_until_expiry": round(info.hours_until_expiry, 1) if info else 0,
            "error": result if not has_token else "",
        }, ensure_ascii=False, indent=2)

    # ---- Tool: status ----------------------------------------------------

    def token_status(
        self,
        account_id: str,
        platform: str,
    ) -> str:
        """
        查询账号凭证状态（不触发刷新）。

        Args:
            account_id: 账号唯一标识
            platform: 平台名

        Returns:
            JSON 格式的状态信息

        Example:
            token_status(account_id="my_weibo", platform="weibo")
        """
        try:
            info = self.manager.get_token_info(account_id, platform)
            if not info:
                return json.dumps({
                    "success": False,
                    "error": f"account_not_found: {account_id}/{platform}",
                }, ensure_ascii=False)

            return json.dumps({
                "success": True,
                **info.to_dict(),
            }, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    # ---- Tool: list -----------------------------------------------------

    def token_list(
        self,
        platform: Optional[str] = None,
        status: Optional[str] = None,
    ) -> str:
        """
        列出所有已注册的账号凭证。

        Args:
            platform: 按平台过滤（weibo / github / twitter 等）
            status: 按状态过滤（active / expiring / expired / invalid）

        Returns:
            JSON 格式的账号列表

        Example:
            token_list(platform="weibo")
            token_list(status="active")
        """
        try:
            tokens = self.manager.list_tokens(platform=platform, status=status)
            return json.dumps({
                "success": True,
                "total": len(tokens),
                "platform": platform or "all",
                "status": status or "all",
                "accounts": [t.to_dict() for t in tokens],
            }, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    # ---- Tool: refresh --------------------------------------------------

    def token_refresh(
        self,
        account_id: str,
        platform: str,
        method: str = "auto",
        cookie_raw: str = "",
        oauth_refresh_token: str = "",
        timeout_sec: int = 120,
    ) -> str:
        """
        主动刷新凭证。

        方法说明：
          - "auto"      : probe → QR → 通知（默认）
          - "probe_only": 只检查是否有效，不刷新
          - "qr_only"   : 强制二维码扫码（仅 weibo）
          - "manual"     : 保存传入的 cookie_raw（需要同时传 cookie_raw 参数）
          - "oauth"      : 用 refresh_token 换新 token（GitHub/Twitter OAuth）

        Args:
            account_id: 账号唯一标识
            platform: 平台名
            method: 刷新方式（默认 auto）
            cookie_raw: 手动模式时传入的新 Cookie
            oauth_refresh_token: OAuth 刷新令牌
            timeout_sec: QR 扫码超时（秒）

        Returns:
            JSON 格式的刷新结果

        Example:
            # 自动刷新
            token_refresh(account_id="my_weibo", platform="weibo")
            # 手动提供 cookie
            token_refresh(account_id="my_weibo", platform="weibo", method="manual",
                         cookie_raw='{"SUB":"_2A":"xxx"}')
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        result = loop.run_until_complete(
            self.manager.refresh_token(
                account_id=account_id,
                platform=platform,
                method=method,
                cookie_raw=cookie_raw,
                oauth_refresh_token=oauth_refresh_token,
            )
        )

        info = self.manager.get_token_info(account_id, platform)
        return json.dumps({
            "success": result.success,
            "method": result.method,
            "expires_at": result.expires_at or (info.expires_at if info else ""),
            "refreshed_at": result.refreshed_at,
            "error": result.error,
        }, ensure_ascii=False, indent=2)

    # ---- Tool: delete ---------------------------------------------------

    def token_delete(
        self,
        account_id: str,
        platform: str,
    ) -> str:
        """
        删除账号凭证。

        Args:
            account_id: 账号唯一标识
            platform: 平台名

        Returns:
            JSON 格式的删除结果

        Example:
            token_delete(account_id="my_weibo", platform="weibo")
        """
        try:
            deleted = self.manager.delete_token(account_id, platform)
            return json.dumps({
                "success": deleted,
                "account_id": account_id,
                "platform": platform,
            }, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    # ---- Tool: check_expiring -------------------------------------------

    def token_check_expiring(
        self,
        dry_run: bool = True,
    ) -> str:
        """
        定时检查所有账号是否即将过期，发送预警 + 触发自动刷新。

        建议配合 Scheduler 每 60 分钟调用一次：
          - dry_run=True  : 只报告，不发通知不刷新（测试用）
          - dry_run=False : 真实执行（发预警 + 自动刷新）

        Returns:
            JSON 格式的检查报告

        Example:
            token_check_expiring(dry_run=True)   # 测试
            token_check_expiring(dry_run=False)   # 真实执行
        """
        try:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            coro = self.manager.check_all_expiring(dry_run=dry_run)
            result = loop.run_until_complete(coro)

            return json.dumps({
                "success": True,
                "dry_run": dry_run,
                **(result or {}),
            }, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)
