#!/usr/bin/env python3
"""
channel_agent_reach_executor.py - TrendRadar 统一渠道执行器

部署路径（容器内）: /app/mcp_server/orchestration/channel_agent_reach_executor.py
注册: 在 channel_task_dispatcher.py 中通过 TYPE_CHECKING 引入

支持三个 template_id（可被 MCP 智能体调用）:
  agent.reach.github_trending   抓取 GitHub Trending 并中文化排序
  agent.reach.leaderboard       查询历史榜单（today/chinese/top-N）
  agent.reach.daily_post        完整流水线：多源抓取 → 翻译 → 发微博

模板 properties:
  github_trending: {"since":"daily","min_period":300,"limit":20}
  leaderboard:     {"action":"today"|"chinese"|"top","days":7}
  daily_post:      {"dry_run":true,"account_ref":"halfking"}
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp_server.services.storage_service import StorageService, get_storage_service


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

TRENDING_URL = "https://github.com/trending"

# 高频技术 topics 中文化映射（本地字典，避免每次都调 LLM）
TOPIC_MAP = {
    "ai": "AI", "ml": "机器学习", "deep-learning": "深度学习",
    "machine-learning": "机器学习", "llm": "大模型", "agent": "智能体",
    "agentic": "智能体", "rag": "RAG", "chatbot": "聊天机器人",
    "openai": "OpenAI", "anthropic": "Anthropic", "claude": "Claude",
    "claude-code": "Claude Code", "chatgpt": "ChatGPT", "gpt": "GPT",
    "framework": "框架", "library": "库", "tool": "工具", "cli": "命令行",
    "api": "API", "sdk": "SDK", "browser": "浏览器", "automation": "自动化",
    "scraping": "爬虫", "stealth": "反检测", "python": "Python",
    "javascript": "JavaScript", "typescript": "TypeScript", "rust": "Rust",
    "go": "Go", "frontend": "前端", "backend": "后端", "database": "数据库",
    "vector-database": "向量数据库", "search": "搜索", "embedding": "嵌入",
    "image": "图像", "video": "视频", "audio": "音频", "voice": "语音",
    "open-source": "开源", "self-hosted": "自部署", "docker": "Docker",
    "kubernetes": "K8s", "security": "安全", "mcp": "MCP",
    "compression": "压缩", "prompt-engineering": "提示工程",
}


def _is_chinese(text: str) -> bool:
    if not text:
        return False
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            return True
    return False


def _translate_topics(topics: List[str]) -> List[str]:
    return [TOPIC_MAP.get(t.lower().strip(), t) for t in (topics or [])[:8]]


def _fetch_trending(
    since: str = "daily",
    spoken_language: str = "",
    timeout: int = 20,
    retries: int = 3,
) -> List[Dict[str, Any]]:
    """抓取 GitHub Trending HTML 并解析（带重试 + IncompleteRead 容错）。"""
    params = {"since": since}
    if spoken_language:
        params["spoken_language_code"] = spoken_language
    url = TRENDING_URL + "?" + urllib.parse.urlencode(params)

    html = ""
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) trendaradar-agent-reach",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                try:
                    raw = resp.read()
                except Exception as read_err:
                    partial = getattr(read_err, "partial", None)
                    if partial:
                        raw = partial
                    else:
                        raise
                html = raw.decode("utf-8", errors="replace")
            if "Box-row" in html and len(html) > 50000:
                break
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1 + attempt)
                continue
    if not html or "Box-row" not in html:
        raise last_err or RuntimeError("trending_fetch_failed: empty html")

    articles = html.split('<article class="Box-row"')[1:]
    repos = []
    for i, a in enumerate(articles, 1):
        name_m = re.search(r'<h2[^>]*>\s*<a[^>]*href="/([^"]+)"', a)
        if not name_m:
            continue
        full_name = name_m.group(1).strip()
        if "/" not in full_name:
            continue
        owner, repo_name = full_name.split("/", 1)

        desc_m = re.search(r'<p class="col-9[^"]*">\s*([^<]+)', a)
        desc = (desc_m.group(1).strip() if desc_m else "").replace("&amp;", "&").replace("&#39;", "'")

        lang_m = re.search(r'<span[^>]*itemprop="programmingLanguage">\s*([^<]+)', a)
        lang = lang_m.group(1).strip() if lang_m else ""

        period_m = re.search(r'(\d[\d,]*)\s*stars\s*(today|this week|this month)', a)
        stars_period = int(period_m.group(1).replace(",", "")) if period_m else 0

        total_stars_m = re.search(
            r'href="/' + re.escape(full_name) + r'/stargazers"[^>]*>\s*([\d,]+)', a
        )
        stars_total = int(total_stars_m.group(1).replace(",", "")) if total_stars_m else 0

        repos.append({
            "name": full_name,
            "owner": owner,
            "repo": repo_name,
            "url": f"https://github.com/{full_name}",
            "description": desc,
            "language": lang,
            "stars_period": stars_period,
            "stars_total": stars_total,
            "period": since,
            "rank": i,
            "is_chinese": _is_chinese(desc),
        })
    return repos


def _score(r: Dict[str, Any]) -> float:
    """中文偏好评分：增速为主 + 中文加分。"""
    s = r.get("stars_period", 0) * 50
    if r.get("is_chinese"):
        s += 10000
    return round(s, 1)


def _save_ranking(repos: List[Dict[str, Any]], output_dir: Path) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = output_dir / f"{today}.json"
    data = {
        "date": today,
        "generated_at": datetime.now().isoformat(),
        "source": "github-trending",
        "total": len(repos),
        "repos": repos,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return str(path)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


@dataclass
class AgentReachExecutorResult:
    action: str = ""
    success: bool = False
    summary: str = ""
    error: str = ""
    artifacts: List[Dict[str, Any]] = field(default_factory=list)
    metrics: Dict[str, Any] = field(default_factory=dict)


class ChannelAgentReachExecutor:
    """Agent Reach 统一渠道执行器。

    三个模板（通过 channel_task_dispatcher 注册）：
      agent.reach.github_trending  抓取 GitHub Trending 并写入 daily ranking
      agent.reach.leaderboard      查询历史榜单（today / chinese / top-N）
      agent.reach.daily_post       完整发帖流水线（趋势抓取 → 翻译 → 发微博）
    """

    SCRIPTS_DIR = Path(
        os.environ.get(
            "AGENT_REACH_SCRIPTS_DIR",
            "/opt/trendaradar/agent_reach/scripts",
        )
    )
    RANKING_DIR = Path(
        os.environ.get(
            "AGENT_REACH_RANKING_DIR",
            "/opt/trendaradar/agent_reach/rankings/daily",
        )
    )

    def __init__(
        self,
        storage: Optional[StorageService] = None,
        config_root: Optional[str] = None,
    ):
        self.storage = storage or get_storage_service()
        self.config_root = Path(config_root or Path(__file__).resolve().parents[3])

    # ---- main entry ------------------------------------------------------

    def run(self, run: Dict[str, Any]) -> Dict[str, Any]:
        properties = run.get("properties") if isinstance(run.get("properties"), dict) else {}
        template_id = str(run.get("template_id") or "")
        result = AgentReachExecutorResult(action=template_id)

        try:
            if template_id == "agent.reach.github_trending":
                self._handle_trending(properties, result)
            elif template_id == "agent.reach.leaderboard":
                self._handle_leaderboard(properties, result)
            elif template_id == "agent.reach.daily_post":
                self._handle_daily_post(properties, result)
            else:
                result.error = f"unsupported_template: {template_id}"
        except Exception as exc:
            result.error = str(exc)

        return self._build_result(result)

    # ---- handlers --------------------------------------------------------

    def _handle_trending(
        self, properties: Dict[str, Any], result: AgentReachExecutorResult
    ) -> None:
        since = str(properties.get("since", "daily"))
        spoken_language = str(properties.get("spoken_language", ""))
        min_period = int(properties.get("min_period", 0))
        limit = int(properties.get("limit", 20))

        # 抓全部 + 中文专属，合并去重
        repos_all = _fetch_trending(since=since, spoken_language="")
        repos_zh = _fetch_trending(since=since, spoken_language="zh")
        seen: Dict[str, Dict[str, Any]] = {}
        for r in repos_all + repos_zh:
            key = r["name"]
            if key not in seen or r["is_chinese"]:
                seen[key] = r
        repos = list(seen.values())

        for r in repos:
            r["score"] = _score(r)
        repos.sort(key=lambda x: x["score"], reverse=True)

        if min_period > 0:
            repos = [r for r in repos if r["stars_period"] >= min_period]
        repos = repos[:limit]

        ranking_path = _save_ranking(repos, self.RANKING_DIR)

        for r in repos:
            result.artifacts.append({
                "artifact_type": "github_trending_repo",
                "name": r["name"],
                "url": r["url"],
                "description": r["description"],
                "language": r["language"],
                "stars_period": r["stars_period"],
                "stars_total": r.get("stars_total", 0),
                "period": r["period"],
                "score": r["score"],
                "is_chinese": r["is_chinese"],
            })

        result.success = True
        result.summary = (
            f"trending: {len(repos)} repos ({since}), "
            f"top1={repos[0]['name'] if repos else 'none'}"
        )
        result.metrics = {
            "since": since,
            "spoken_language_filter": spoken_language,
            "min_period": min_period,
            "total": len(repos),
            "chinese_count": sum(1 for r in repos if r["is_chinese"]),
            "ranking_path": ranking_path,
        }

    def _handle_leaderboard(
        self, properties: Dict[str, Any], result: AgentReachExecutorResult
    ) -> None:
        action = str(properties.get("action", "today"))

        if action == "today":
            today = datetime.now().strftime("%Y-%m-%d")
            data = self._load_ranking(today)
            if not data:
                result.error = f"no_ranking_for_{today}"
                return
            repos = data.get("repos", [])[:20]

        elif action == "chinese":
            today = datetime.now().strftime("%Y-%m-%d")
            data = self._load_ranking(today)
            if not data:
                result.error = f"no_ranking_for_{today}"
                return
            repos = [r for r in data.get("repos", []) if r.get("is_chinese")][:15]

        elif action == "top":
            days = int(properties.get("days", 7))
            project_scores: Dict[str, Dict[str, Any]] = {}
            for offset in range(days):
                date = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
                data = self._load_ranking(date)
                for r in data.get("repos", []):
                    key = r["name"]
                    if key not in project_scores:
                        project_scores[key] = {**r, "cum_score": 0, "appearances": 0}
                    project_scores[key]["cum_score"] += r.get("score", 0)
                    project_scores[key]["appearances"] += 1
            repos = sorted(
                project_scores.values(), key=lambda x: x["cum_score"], reverse=True
            )[:20]
            result.metrics["days"] = days

        else:
            result.error = f"unsupported_action: {action}"
            return

        for r in repos:
            result.artifacts.append({
                "artifact_type": "leaderboard_entry",
                "name": r["name"],
                "url": r.get("url", ""),
                "description": r.get("description", ""),
                "score": r.get("score", 0),
                "stars_period": r.get("stars_period", 0),
                "is_chinese": r.get("is_chinese", False),
                "appearances": r.get("appearances"),
            })
        result.success = True
        result.summary = f"leaderboard[{action}]: {len(repos)} entries"
        result.metrics.update({"action": action, "total": len(repos)})

    def _handle_daily_post(
        self, properties: Dict[str, Any], result: AgentReachExecutorResult
    ) -> None:
        dry_run = bool(properties.get("dry_run", True))
        script_path = self.SCRIPTS_DIR / "daily-x-to-weibo.py"

        if not script_path.exists():
            result.error = f"script_not_found: {script_path}"
            return

        env = os.environ.copy()
        if dry_run:
            env["WEIBO_DRY_RUN"] = "1"

        venv_python = Path("/root/.agent-reach-venv/bin/python3")
        if not venv_python.exists():
            venv_python = Path(sys.executable)

        try:
            proc = subprocess.run(
                [str(venv_python), str(script_path)],
                capture_output=True,
                text=True,
                timeout=600,
                env=env,
            )

            result_json_path = Path("/tmp/daily-weibo-post.json")
            post_data = {}
            if result_json_path.exists():
                with open(result_json_path) as f:
                    post_data = json.load(f)

            success = proc.returncode == 0
            tail_stdout = (proc.stdout or "")[-300:]
            result.success = success
            result.summary = (
                post_data.get("meta", {}).get("hook", "")[:120]
                or tail_stdout.splitlines()[-1][:120] if tail_stdout else ""
            )
            result.artifacts.append({
                "artifact_type": "weibo_post",
                "content": post_data.get("content", ""),
                "github_url": post_data.get("github_url", ""),
                "meta": post_data.get("meta", {}),
                "dry_run": dry_run,
            })
            result.metrics = {
                "return_code": proc.returncode,
                "dry_run": dry_run,
                "stdout_lines": len(proc.stdout.splitlines()),
            }
            if not success:
                result.error = (
                    f"script_failed: rc={proc.returncode}, "
                    f"tail={proc.stdout[-200:] + proc.stderr[-100:]}"
                )
        except subprocess.TimeoutExpired:
            result.error = "script_timeout (10min)"
        except Exception as exc:
            result.error = str(exc)

    # ---- utilities -------------------------------------------------------

    def _load_ranking(self, date_str: str) -> Dict[str, Any]:
        path = self.RANKING_DIR / f"{date_str}.json"
        if not path.exists():
            return {}
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _build_result(self, result: AgentReachExecutorResult) -> Dict[str, Any]:
        return {
            "status": "success" if not result.error else "failed",
            "summary": result.summary
            or f"agent.reach: {result.action} | err={result.error}",
            "artifacts": result.artifacts or [],
            "metrics": result.metrics or {},
            "error": result.error,
        }