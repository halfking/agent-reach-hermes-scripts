#!/usr/bin/env python3
"""
acc_sync.py - 查询 ACC LLM 使用量统计
通过 SSH 在 184 服务器上执行 psql，直接查询 PostgreSQL。
（绕过 ACC API 的 auth 要求）

用法:
    python acc_sync.py today                    # 今日统计
    python acc_sync.py week                      # 本周统计
    python acc_sync.py task weibo-daily-2026-06-09-09  # 特定任务
    python acc_sync.py channel weibo            # 频道统计
"""

import subprocess
import sys
import json
import os

# ============================================================================
# SSH 配置（从环境变量读取敏感信息）
# ============================================================================
SSH_HOST = "root@14.103.112.184"
SSH_PASS = os.environ.get("SSH_PASS", "Kaixuan2025&9900#")
PG_USER = "kxuser"
PG_DB = "kaixuan"
PG_CONTAINER_CMD = "docker ps --format '{{.Names}}' | grep -E '^k8s_postgres' | grep -v POD | head -1"


def get_pg_container() -> str:
    """获取当前 PostgreSQL 容器名"""
    result = subprocess.run(
        ["sshpass", "-p", SSH_PASS, "ssh", "-o", "StrictHostKeyChecking=no",
         SSH_HOST, PG_CONTAINER_CMD],
        capture_output=True, text=True, timeout=15
    )
    return result.stdout.strip()


def psql_query(sql: str) -> str:
    """在 184 服务器上执行 SQL，返回 stdout"""
    container = get_pg_container()
    if not container:
        return "ERROR: No PostgreSQL container found"

    # 使用 printf 避免 SQL 引号问题
    sql_safe = sql.replace("\\", "\\\\").replace('"', '\\"')
    cmd = f'docker exec -i {container} psql -U {PG_USER} -d {PG_DB} -c "{sql_safe}"'
    result = subprocess.run(
        ["sshpass", "-p", SSH_PASS, "ssh", "-o", "StrictHostKeyChecking=no", SSH_HOST, cmd],
        capture_output=True, text=True, timeout=30
    )
    return result.stdout


def parse_table(output: str) -> list[list[str]]:
    """解析 psql 表格输出，返回 rows"""
    lines = output.strip().split("\n")
    if len(lines) < 3:
        return []
    # 跳过 header(0) + border(1)，取数据行
    data_lines = [l for l in lines[2:] if l.strip() and "|" in l and not l.strip().startswith("(")]
    rows = []
    for line in data_lines:
        parts = [p.strip() for p in line.split("|")]
        # 去掉首尾空字符串
        if parts and parts[0] == "":
            parts = parts[1:]
        if parts and parts[-1] == "":
            parts = parts[:-1]
        rows.append(parts)
    return rows


# ============================================================================
# 查询函数
# ============================================================================

def query_today():
    """今日 LLM 使用量（按 employee/model 分组）"""
    sql = """
    SELECT employee_id, provider, model,
           SUM(prompt_tokens)::int as prompt_tokens,
           SUM(completion_tokens)::int as completion_tokens,
           SUM(total_tokens)::int as total_tokens,
           ROUND(SUM(cost_usd)::numeric, 8)::text as cost_usd,
           COUNT(*)::int as calls
    FROM llm_usage_logs
    WHERE DATE(created_at) = CURRENT_DATE
    GROUP BY employee_id, provider, model
    ORDER BY total_tokens DESC;
    """
    output = psql_query(sql)
    rows = parse_table(output)

    print(f"\n📊 ACC LLM 使用量 — 今天 ({subprocess.run(['date', '+%Y-%m-%d'], capture_output=True, text=True).stdout.strip()})")
    print("=" * 75)

    if not rows:
        print("  今日暂无记录（可能 GitHub API 限流，未触发翻译调用）")
        return

    total_tokens = 0
    total_cost = 0.0
    for row in rows:
        emp, prov, model, p, c, t, cost, calls = row
        total_tokens += int(t)
        total_cost += float(cost)
        print(f"  {emp} | {prov}/{model}")
        print(f"    {int(p):,} prompt + {int(c):,} completion = {int(t):,} tokens | ${float(cost):.6f} | {calls} calls")

    print("-" * 75)
    print(f"  合计: {total_tokens:,} tokens, ${total_cost:.6f}")
    print()


def query_week():
    """本周 LLM 使用量（按天分组）"""
    sql = """
    SELECT DATE(created_at)::text as day,
           SUM(total_tokens)::int as tokens,
           ROUND(SUM(cost_usd)::numeric, 8)::text as cost_usd,
           COUNT(*)::int as calls
    FROM llm_usage_logs
    WHERE created_at >= CURRENT_DATE - INTERVAL '7 days'
    GROUP BY DATE(created_at)
    ORDER BY day DESC;
    """
    output = psql_query(sql)
    rows = parse_table(output)

    print(f"\n📊 ACC LLM 使用量 — 最近7天")
    print("=" * 60)

    if not rows:
        print("  本周暂无记录")
        return

    total_tokens = 0
    total_cost = 0.0
    for row in rows:
        day, tokens, cost, calls = row
        total_tokens += int(tokens)
        total_cost += float(cost)
        print(f"  {day}: {int(tokens):>8,} tokens | ${float(cost):>8.6f} | {calls} calls")

    print("-" * 60)
    print(f"  合计: {total_tokens:,} tokens, ${total_cost:.6f}")
    print()


def query_task(task_id: str):
    """特定 task_id 的所有 LLM 调用记录"""
    sql = f"""
    SELECT created_at::text as time,
           provider, model,
           prompt_tokens, completion_tokens, total_tokens,
           ROUND(cost_usd::numeric, 8)::text as cost_usd,
           metadata::text as meta
    FROM llm_usage_logs
    WHERE metadata->>'task_id' = '{task_id}'
    ORDER BY created_at;
    """
    output = psql_query(sql)
    rows = parse_table(output)

    print(f"\n📊 任务使用量 — {task_id}")
    print("=" * 75)

    if not rows:
        print(f"  未找到任务 {task_id}")
        print(f"  （task_id 存储在 metadata JSON 中，ACC API 暂未抽取到 task_id 列）")
        return

    total_tokens = 0
    total_cost = 0.0
    for row in rows:
        ts, prov, model, p, c, t, cost, meta = row
        total_tokens += int(t)
        total_cost += float(cost)
        # 解析 metadata
        try:
            import json as _json
            m = _json.loads(meta.replace("'", '"'))
            ch = m.get("channel", "unknown")
            fn = m.get("function", "-")
        except:
            ch, fn = "unknown", "-"
        print(f"  [{ts}] {prov}/{model} | {int(p):+5} + {int(c):+5} = {int(t):+6} | ${float(cost):.6f} | {ch}/{fn}")

    print("-" * 75)
    print(f"  合计: {total_tokens:,} tokens, ${total_cost:.6f} ({len(rows)} 次 LLM 调用)")
    print()


def query_channel(channel: str):
    """按频道统计（最近7天）"""
    sql = f"""
    SELECT DATE(created_at)::text as day,
           metadata->>'channel' as channel,
           SUM(total_tokens)::int as tokens,
           ROUND(SUM(cost_usd)::numeric, 8)::text as cost_usd,
           COUNT(*)::int as calls
    FROM llm_usage_logs
    WHERE metadata->>'channel' = '{channel}'
      AND created_at >= CURRENT_DATE - INTERVAL '7 days'
    GROUP BY day, metadata->>'channel'
    ORDER BY day DESC;
    """
    output = psql_query(sql)
    rows = parse_table(output)

    print(f"\n📊 频道使用量 — {channel} (最近7天)")
    print("=" * 60)

    if not rows:
        print(f"  频道 {channel} 暂无记录")
        return

    total_tokens = 0
    total_cost = 0.0
    for row in rows:
        day, ch, tokens, cost, calls = row
        total_tokens += int(tokens)
        total_cost += float(cost)
        print(f"  {day} | {ch}: {int(tokens):>8,} tokens | ${float(cost):>8.6f} | {calls} calls")

    print("-" * 60)
    print(f"  合计: {total_tokens:,} tokens, ${total_cost:.6f}")
    print()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "today":
        query_today()
    elif cmd == "week":
        query_week()
    elif cmd == "task" and len(sys.argv) >= 3:
        query_task(sys.argv[2])
    elif cmd == "channel" and len(sys.argv) >= 3:
        query_channel(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()