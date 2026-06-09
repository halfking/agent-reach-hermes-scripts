"""
llm_tracker.py - LLM 调用追踪模块
===============================
包装 LLM 调用，自动捕获 usage 并写入 ACC。

用法:
    from llm_tracker import LLMWrapper, create_task_session, post_usage

    # 1. 创建任务 session（每次 cron run 调用一次）
    task_id = create_task_session(
        channel="weibo",
        run_id="2026-06-09-09",
        description="Twitter AI 项目 → 微博转发"
    )

    # 2. 用 LLMWrapper 包装 LLM 调用
    wrapper = LLMWrapper(task_id=task_id, provider="evol", model="minimax-m2.7")
    result = wrapper.chat("翻译: Hello world")

    # 3. 自动写入 ACC（也可手动 post_usage）
"""

import json
import os
import time
import uuid
import hmac
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ============================================================================
# 配置
# ============================================================================

ACC_BASE_URL = os.environ.get("ACC_API_URL", "https://acc.kxpms.cn")
ACC_LLM_USAGE_URL = f"{ACC_BASE_URL}/api/acc/llm-usage"
ACC_LLM_SUMMARY_URL = f"{ACC_BASE_URL}/api/acc/llm-summary"
ACC_SESSIONS_URL = f"{ACC_BASE_URL}/api/acc/sessions"

# LM-Gateway 自有通道（llm.kxpms.cn）配置
LM_GATEWAY_URL = os.environ.get("LM_GATEWAY_URL", "https://llm.kxpms.cn")
LM_GATEWAY_API_URL = f"{LM_GATEWAY_URL}/v1/chat/completions"
LM_GATEWAY_AUTH_URL = f"{LM_GATEWAY_URL}/api/auth/token"
LM_GATEWAY_USERNAME = os.environ.get("LM_GATEWAY_USER", "admin")
LM_GATEWAY_PASSWORD = os.environ.get("LM_GATEWAY_PASS", "Veritrans&9527")

# MiniMax (EVOL) 定价 (USD per 1M tokens)
MINIMAX_PRICING = {
    "input": 0.05,    # $0.05 per 1M input tokens
    "output": 0.2,    # $0.20 per 1M output tokens
}

# ============================================================================
# LM-Gateway Token 管理（llm.kxpms.cn 自有通道）
# ============================================================================
_lm_token_cache = {"token": None, "expires_at": 0}


def get_lm_gateway_token() -> str:
    """
    获取 llm.kxpms.cn 的 Bearer token，带内存缓存（有效期 55 分钟）。
    先 POST /api/auth/token 获取，再缓存。
    """
    import time as _time
    now = _time.time()
    if _lm_token_cache["token"] and now < _lm_token_cache["expires_at"]:
        return _lm_token_cache["token"]

    payload = json.dumps({"username": LM_GATEWAY_USERNAME, "password": LM_GATEWAY_PASSWORD}).encode()
    req = urllib.request.Request(
        LM_GATEWAY_AUTH_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        token = data.get("api_key", "")
        if not token:
            raise RuntimeError(f"LM-Gateway 未返回 api_key: {data}")
        # 缓存 55 分钟（token 有效期通常 1 小时）
        _lm_token_cache["token"] = token
        _lm_token_cache["expires_at"] = now + 55 * 60
        log(f"[LLM Tracker] LM-Gateway token 已刷新，有效期至 {int(_lm_token_cache['expires_at']/60)} 分钟")
        return token
    except Exception as e:
        # 缓存失效时返回旧 token 尝试（降级）
        if _lm_token_cache["token"]:
            log(f"[LLM Tracker] ⚠️ Token 刷新失败，使用缓存: {e}")
            return _lm_token_cache["token"]
        raise RuntimeError(f"LM-Gateway token 获取失败: {e}") from e


# ============================================================================
# ACC Token 管理（HMAC-SHA256 签名，与 sign-acc-admin-token.cjs 同算法）
# ============================================================================
_ACC_TOKEN_SECRET = os.environ.get("ACC_AUTH_SECRET", "fe5593bhp0VJBLy9meLtx4PObqfeK0sMBNb37MOdwxg=")


def _sign_acc_token(username: str = "admin", ttl_hours: int = 8) -> str:
    """
    签发 ACC admin 本地 JWT（HMAC-SHA256），与 sign-acc-admin-token.cjs 同算法。
    用于 ACC API 的 Bearer Token 认证。
    """
    import base64 as _base64
    exp = int(time.time() * 1000) + ttl_hours * 3600 * 1000
    payload = json.dumps({"sub": username, "exp": exp})
    encoded = _base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac.new(
        _ACC_TOKEN_SECRET.encode(),
        encoded.encode(),
        hashlib.sha256
    ).digest()
    sig_encoded = _base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"{encoded}.{sig_encoded}"


def get_acc_admin_token() -> str:
    """获取 ACC admin Bearer token（模块级缓存，8小时刷新）"""
    import time as _time
    if not hasattr(get_acc_admin_token, "_cache"):
        get_acc_admin_token._cache = {"token": None, "expires_at": 0}
    cache = get_acc_admin_token._cache
    now = _time.time()
    if cache["token"] and now < cache["expires_at"]:
        return cache["token"]
    token = _sign_acc_token(ttl_hours=8)
    cache["token"] = token
    cache["expires_at"] = now + 7 * 3600  # 7小时后刷新（留1小时余量）
    log(f"[LLM Tracker] ACC admin token 已签发，有效期 8h")
    return token


# ============================================================================
# 全局 task_id（每次 cron run 设置一次）
# ============================================================================
_current_task_id = None
_current_channel = None
_current_run_id = None


# ============================================================================
# 工具函数
# ============================================================================

def generate_task_id(channel: str, run_id: str = None) -> str:
    """生成唯一 task_id: {channel}-daily-{date}-{run}"""
    if run_id is None:
        run_id = datetime.now().strftime("%Y-%m-%d-%H")
    return f"{channel}-daily-{run_id}"


def create_task_session(
    channel: str,
    run_id: str = None,
    description: str = "",
    employee_id: str = "hermes-01",
) -> str:
    """
    创建任务 session，返回 task_id。
    每次 cron run 调用一次。

    Args:
        channel: 频道名称（weibo/xhs/zhihu/twitter）
        run_id: 运行ID，默认格式: YYYY-MM-DD-HH
        description: 任务描述

    Returns:
        task_id 字符串
    """
    global _current_task_id, _current_channel, _current_run_id

    task_id = generate_task_id(channel, run_id)
    _current_task_id = task_id
    _current_channel = channel
    _current_run_id = run_id or datetime.now().strftime("%Y-%m-%d-%H")

    log(f"[LLM Tracker] 创建任务 session: task_id={task_id}, channel={channel}")

    # 可选：创建 ACC session（ACC 的 sessions API 需要认证，失败不阻塞）
    try:
        payload = {
            "employee_id": employee_id,
            "session_type": "task",
            "metadata": {
                "task_id": task_id,
                "channel": channel,
                "description": description,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        }
        req = urllib.request.Request(
            ACC_SESSIONS_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {get_acc_admin_token()}",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            log(f"[LLM Tracker] ACC session 创建成功: {result.get('session', {}).get('id')}")
    except Exception as e:
        log(f"[LLM Tracker] ACC session 创建失败（不影响主流程）: {e}")

    return task_id


def calculate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """根据 token 数量计算 USD 成本"""
    cost = (
        prompt_tokens * MINIMAX_PRICING["input"] / 1_000_000 +
        completion_tokens * MINIMAX_PRICING["output"] / 1_000_000
    )
    return round(cost, 6)


def log(msg: str):
    """打印带时间戳的日志"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}")


# ============================================================================
# 核心：写入 ACC
# ============================================================================

def post_usage(
    task_id: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    cost_usd: float = None,
    employee_id: str = "hermes-01",
    metadata: dict = None,
) -> bool:
    """
    将 LLM 使用量写入 ACC /api/acc/llm-usage

    Args:
        task_id: 任务ID（如 weibo-daily-2026-06-09-09）
        provider: LLM 提供商（如 evol/minimax）
        model: 模型名（如 minimax-m2.7）
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数
        total_tokens: 总 token 数
        cost_usd: USD 成本（自动计算可填 None）
        employee_id: Agent ID
        metadata: 附加数据（如 channel, github_url 等）

    Returns:
        True 成功，False 失败
    """
    if cost_usd is None:
        cost_usd = calculate_cost(prompt_tokens, completion_tokens)

    payload = {
        "employee_id": employee_id,
        "llm_provider": provider,
        "llm_model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cost_usd": cost_usd,
        "metadata": {
            "task_id": task_id,
            "channel": _current_channel or "unknown",
            **(metadata or {})
        }
    }

    try:
        req = urllib.request.Request(
            ACC_LLM_USAGE_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {get_acc_admin_token()}",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            log(f"[LLM Tracker] ✅ 使用量写入 ACC: task_id={task_id}, tokens={total_tokens}, cost=${cost_usd:.6f}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        log(f"[LLM Tracker] ⚠️ ACC 写入失败 HTTP {e.code}: {body}")
        return False
    except Exception as e:
        log(f"[LLM Tracker] ⚠️ ACC 写入失败: {e}")
        return False


# ============================================================================
# LLM 调用包装器
# ============================================================================

class LLMWrapper:
    """
    包装 LLM 调用，自动追踪 usage 并写入 ACC。

    用法:
        wrapper = LLMWrapper(task_id="weibo-daily-2026-06-09-09")
        result = wrapper.chat("翻译: Hello world")
        print(result)  # {'content': '...', 'usage': {...}}
    """

    # 统一走 llm.kxpms.cn 自有通道（不走 mg-new.evolai.cn 收费通道）
    API_URL = LM_GATEWAY_API_URL

    def __init__(
        self,
        task_id: str = None,
        provider: str = "evol",
        model: str = "minimax-m2.7",
        employee_id: str = "hermes-01",
        system_prompt: str = None,
    ):
        self.task_id = task_id or _current_task_id or "unknown"
        self.provider = provider
        self.model = model
        self.employee_id = employee_id
        self.system_prompt = system_prompt
        self.total_usage = {"prompt": 0, "completion": 0, "total": 0, "cost": 0.0, "calls": 0}

    def chat(
        self,
        user_message: str,
        max_tokens: int = 800,
        temperature: float = 0.7,
        metadata: dict = None,
    ) -> dict:
        """
        发送 chat 请求，自动追踪 usage。

        Returns:
            {
                'content': str,          # LLM 回复内容
                'usage': {                # 使用量信息
                    'prompt_tokens': int,
                    'completion_tokens': int,
                    'total_tokens': int,
                    'cost_usd': float,
                },
                'raw': {...}              # 原始 API 响应
            }
        """
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        req = urllib.request.Request(
            self.API_URL,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {get_lm_gateway_token()}",
            },
            method="POST"
        )

        start_time = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = json.loads(resp.read())
        except Exception as e:
            raise RuntimeError(f"LLM 调用失败: {e}") from e

        latency_ms = int((time.time() - start_time) * 1000)

        # 解析 response（EVOL 包装在 body 里）
        body = raw.get("body", raw)
        choices = body.get("choices", [])
        content = choices[0]["message"]["content"] if choices else ""

        usage = body.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)
        cost_usd = calculate_cost(prompt_tokens, completion_tokens)

        # 更新累计
        self.total_usage["prompt"] += prompt_tokens
        self.total_usage["completion"] += completion_tokens
        self.total_usage["total"] += total_tokens
        self.total_usage["cost"] += cost_usd
        self.total_usage["calls"] += 1

        # 写入 ACC（异步不阻塞，但同步等待以便调试）
        post_usage(
            task_id=self.task_id,
            provider=self.provider,
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            employee_id=self.employee_id,
            metadata={
                "latency_ms": latency_ms,
                **(metadata or {})
            }
        )

        return {
            "content": content,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cost_usd": cost_usd,
            },
            "raw": body,
        }

    def get_total_usage(self) -> dict:
        """获取本次 Wrapper 生命周期内的累计使用量"""
        return dict(self.total_usage)


# ============================================================================
# 统计查询
# ============================================================================

def query_usage_summary(
    start_date: str = None,
    end_date: str = None,
    group_by: str = "date",
    employee_id: str = "hermes-01",
) -> dict:
    """
    查询 ACC LLM 使用量汇总

    Args:
        start_date: 开始日期 YYYY-MM-DD
        end_date: 结束日期 YYYY-MM-DD
        group_by: date / employee / model
        employee_id: 过滤特定 agent

    Returns:
        ACC API 响应 dict
    """
    params = f"group_by={group_by}"
    if start_date:
        params += f"&start_date={start_date}"
    if end_date:
        params += f"&end_date={end_date}"
    if employee_id:
        params += f"&employee_id={employee_id}"

    url = f"{ACC_LLM_SUMMARY_URL}?{params}"
    log(f"[LLM Tracker] 查询 ACC: {url}")

    try:
        req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {get_acc_admin_token()}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        log(f"[LLM Tracker] ⚠️ 查询失败: {e}")
        return {"ok": False, "error": str(e)}


def query_task_usage(task_id: str) -> dict:
    """查询特定 task_id 的使用量"""
    # 通过 metadata.task_id 过滤需要查 llm-usage（不支持 task_id 直接过滤）
    # 先用 date 范围缩小范围
    date_part = task_id.split("-")[-3:]  # YYYY-MM-DD
    if len(date_part) == 3:
        start_date = "-".join(date_part)
        end_date = start_date
    else:
        start_date = None
        end_date = None

    result = query_usage_summary(start_date=start_date, end_date=end_date, group_by="date")
    return result


def print_usage_report(summary: dict):
    """打印使用量报告（友好格式）"""
    if not summary.get("ok"):
        print(f"查询失败: {summary.get('error')}")
        return

    entries = summary.get("summary", [])
    if not entries:
        print("暂无使用量记录")
        return

    print("\n📊 LLM 使用量报告")
    print("=" * 60)
    total_cost = 0.0
    total_tokens = 0

    for entry in entries:
        if "date" in entry:
            label = entry["date"]
        elif "employee_id" in entry:
            label = f"{entry['employee_id']} ({entry.get('model', 'unknown')})"
        else:
            label = entry.get("model", "unknown")

        tokens = int(entry.get("total_tokens", 0))
        cost = float(entry.get("total_cost", 0))
        calls = int(entry.get("call_count", 0))
        total_cost += cost
        total_tokens += tokens

        print(f"  {label}: {tokens:,} tokens, ${cost:.4f}, {calls} calls")

    print("-" * 60)
    print(f"  合计: {total_tokens:,} tokens, ${total_cost:.4f}")
    print()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--report":
        # 查询今日使用量
        today = datetime.now().strftime("%Y-%m-%d")
        summary = query_usage_summary(start_date=today, end_date=today, group_by="date")
        print_usage_report(summary)
    elif len(sys.argv) > 1 and sys.argv[1] == "--test":
        # 测试写入
        task_id = create_task_session(channel="test", run_id="2026-06-09-test")
        result = LLMWrapper(task_id=task_id).chat("Say 'OK' in one word")
        print(f"回复: {result['content']}")
        print(f"使用量: {result['usage']}")
        print(f"累计: {LLMWrapper(task_id=task_id).total_usage}")
    else:
        print("用法:")
        print("  python llm_tracker.py --report   # 查询今日使用量")
        print("  python llm_tracker.py --test     # 测试写入")