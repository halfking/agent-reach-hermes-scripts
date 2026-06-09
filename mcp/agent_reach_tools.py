"""
Agent Reach 工具 - MCP 封装

通过 MCP 协议向外部智能体开放 Agent Reach 的抓取/翻译/发布能力，
数据落地到 TrendRadar 统一存储（crawl_records + rankings JSON）。

对外暴露的工具：
  - fetch_github_trending      抓取 GitHub Trending，写入 DB + rankings JSON
  - query_leaderboard         查询历史榜单（today/chinese/top-N）
  - run_daily_agent_reach     完整流水线：多源抓取 → 翻译 → 发帖（dry-run 模式）
  - save_agent_reach_data      外部智能体上报 Agent Reach 相关数据到 TrendRadar DB
  - search_agent_reach_news    从 TrendRadar DB 检索已存储的 Agent Reach 数据
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..services.storage_service import StorageService, get_storage_service


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRENDING_URL = "https://github.com/trending"

# 高频技术 topics 中文化映射
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
    "generative-ai": "生成式AI", "artificial-intelligence": "人工智能",
    "neural-network": "神经网络", "transformer": "Transformer",
    "chatbot": "聊天机器人", "automation": "自动化", "bot": "机器人",
    "web": "Web", "webapp": "Web应用", "saas": "SaaS",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_chinese(text: str) -> bool:
    if not text:
        return False
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            return True
    return False


def _github_headers() -> Dict[str, str]:
    """构造 GitHub API 请求头，自动注入 token 提升 rate limit。"""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", "")
    if not token:
        try:
            token = subprocess.check_output(
                ["gh", "auth", "token"], text=True, stderr=subprocess.DEVNULL
            ).strip()
        except Exception:
            token = ""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) trendradar-agent-reach",
        "Accept": "application/vnd.github+json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


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
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) trendradar-agent-reach",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                try:
                    raw = resp.read()
                except Exception as read_err:
                    partial = getattr(read_err, "partial", None)
                    raw = partial if partial else b""
                html = raw.decode("utf-8", errors="replace")
            if "Box-row" in html and len(html) > 50000:
                break
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1 + attempt)
    if not html or "Box-row" not in html:
        raise last_err or RuntimeError("trending_fetch_failed: empty html")

    articles = html.split('<article class="Box-row"')[1:]
    repos = []
    for a in articles:
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

        repos.append({
            "name": full_name,
            "owner": owner,
            "repo": repo_name,
            "url": f"https://github.com/{full_name}",
            "description": desc,
            "language": lang,
            "stars_period": stars_period,
            "stars_total": 0,
            "period": since,
            "is_chinese": _is_chinese(desc),
        })
    return repos


def _score(r: Dict[str, Any]) -> float:
    s = r.get("stars_period", 0) * 50.0
    if r.get("is_chinese"):
        s += 10000.0
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


def _github_repo_info(owner: str, repo: str) -> Dict[str, Any]:
    """通过 GitHub API 获取仓库详细信息。"""
    url = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        req = urllib.request.Request(url, headers=_github_headers())
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return {
            "stars": data.get("stargazers_count", 0),
            "forks": data.get("forks_count", 0),
            "topics": data.get("topics", []),
            "homepage": data.get("homepage") or "",
            "language": data.get("language") or "",
            "description": data.get("description") or "",
            "open_issues": data.get("open_issues_count", 0),
            "license": (data.get("license") or {}).get("name", ""),
            "created_at": data.get("created_at", ""),
            "pushed_at": data.get("pushed_at", ""),
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Tool Class
# ---------------------------------------------------------------------------

class AgentReachTools:
    """Agent Reach MCP 工具类"""

    SCRIPTS_DIR = Path("/opt/trendaradar/agent_reach/scripts")
    RANKING_DIR = Path("/opt/trendaradar/agent_reach/rankings/daily")
    DB_RANKING_DIR = Path("/app/data/agent_reach/rankings/daily")

    def __init__(self, project_root: Optional[str] = None):
        self.project_root = project_root
        self._storage: Optional[StorageService] = None

    @property
    def storage(self) -> StorageService:
        if self._storage is None:
            self._storage = get_storage_service(project_root=self.project_root)
        return self._storage

    # ---- Tool 1: fetch_github_trending ---------------------------------

    def fetch_github_trending(
        self,
        since: str = "daily",
        spoken_language: str = "",
        min_period: int = 0,
        limit: int = 20,
        persist_db: bool = True,
    ) -> str:
        """
        抓取 GitHub Trending 并写入 TrendRadar DB + rankings JSON。

        数据写入两个地方：
          1. TrendRadar PostgreSQL (crawl_records) — 可通过 search_news_unified 查询
          2. /app/data/agent_reach/rankings/daily/YYYY-MM-DD.json — 榜单持久化

        Args:
            since: 时间范围，daily | weekly | monthly（默认 daily）
            spoken_language: 语言过滤，空字符串表示所有语言，'zh' 表示仅中文
            min_period: 最低日均 stars 过滤（默认 0，不过滤）
            limit: 返回条数上限（默认 20）
            persist_db: 是否写入 TrendRadar DB（默认 True，关闭可加快响应）

        Returns:
            JSON 格式的抓取结果（含 repo 列表 + 统计信息）

        Example:
            fetch_github_trending(since="daily", min_period=300, limit=10)
        """
        try:
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

            # 补充 GitHub API 信息（前 3 个）
            for r in repos[:3]:
                info = _github_repo_info(r["owner"], r["repo"])
                r["stars_total"] = info.get("stars", 0)
                r["forks"] = info.get("forks", 0)
                r["topics"] = info.get("topics", [])

            # 写入 rankings JSON（两个路径）
            for dir_path in [self.RANKING_DIR, self.DB_RANKING_DIR]:
                _save_ranking(repos, dir_path)

            # 写入 TrendRadar DB（crawl_records）
            saved_count = 0
            if persist_db:
                for r in repos[:10]:
                    try:
                        self.storage.save_crawl_record(
                            url=r["url"],
                            title=r["name"],
                            content=f"{r['description']}\n\nTopics: {', '.join(r.get('topics', []))}",
                            source_name="github_trending",
                            source_type="github",
                            keywords_matched=_translate_topics(r.get("topics", [])),
                            relevance_score=r["score"] / 1000.0,
                            metadata={
                                "stars_period": r["stars_period"],
                                "stars_total": r.get("stars_total", 0),
                                "language": r["language"],
                                "period": r["period"],
                                "is_chinese": r["is_chinese"],
                                "owner": r["owner"],
                                "repo": r["repo"],
                            },
                        )
                        saved_count += 1
                    except Exception as e:
                        pass

            chinese_count = sum(1 for r in repos if r["is_chinese"])
            return json.dumps({
                "success": True,
                "source": "github_trending",
                "since": since,
                "total_repos_found": len(seen),
                "total_returned": len(repos),
                "chinese_count": chinese_count,
                "db_records_saved": saved_count,
                "ranking_saved_to": str(self.DB_RANKING_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.json"),
                "repos": [
                    {
                        "name": r["name"],
                        "url": r["url"],
                        "description": r["description"],
                        "language": r["language"],
                        "stars_period": r["stars_period"],
                        "stars_total": r.get("stars_total", 0),
                        "topics": r.get("topics", []),
                        "score": r["score"],
                        "is_chinese": r["is_chinese"],
                    }
                    for r in repos
                ],
            }, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    # ---- Tool 2: query_leaderboard ------------------------------------

    def query_leaderboard(
        self,
        action: str = "today",
        days: int = 7,
        limit: int = 20,
    ) -> str:
        """
        查询 GitHub Trending 历史榜单。

        Args:
            action: 查询类型
              - "today": 今日榜单（默认）
              - "chinese": 中文项目榜单（当日）
              - "top": 近 N 天累计 Top（配合 days 参数，默认 7 天）
            days: top 榜单的统计天数（默认 7，仅 action=top 时有效）
            limit: 返回条数上限（默认 20）

        Returns:
            JSON 格式的榜单数据

        Example:
            query_leaderboard(action="today")
            query_leaderboard(action="top", days=7)
            query_leaderboard(action="chinese")
        """
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            ranking_dir = self.DB_RANKING_DIR

            if action == "today":
                data = self._load_ranking(today)
                repos = (data.get("repos", []) if data else [])[:limit]

            elif action == "chinese":
                data = self._load_ranking(today)
                repos = [r for r in (data.get("repos", []) if data else []) if r.get("is_chinese")][:limit]

            elif action == "top":
                project_scores: Dict[str, Dict[str, Any]] = {}
                for offset in range(days):
                    date = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
                    data = self._load_ranking(date)
                    for r in (data.get("repos", []) if data else []):
                        key = r["name"]
                        if key not in project_scores:
                            project_scores[key] = {**r, "cum_score": 0.0, "appearances": 0}
                        project_scores[key]["cum_score"] += r.get("score", 0)
                        project_scores[key]["appearances"] += 1
                repos = sorted(
                    project_scores.values(),
                    key=lambda x: x["cum_score"],
                    reverse=True
                )[:limit]

            else:
                return json.dumps({
                    "success": False,
                    "error": f"unsupported_action: {action}",
                    "hint": "action 可选: today | chinese | top",
                }, ensure_ascii=False)

            if not repos:
                return json.dumps({
                    "success": False,
                    "error": f"no_data_for_{action}",
                    "hint": "尝试先调用 fetch_github_trending 抓取数据",
                }, ensure_ascii=False)

            return json.dumps({
                "success": True,
                "action": action,
                "days": days if action == "top" else 1,
                "total": len(repos),
                "repos": [
                    {
                        "name": r["name"],
                        "url": r.get("url", ""),
                        "description": r.get("description", ""),
                        "language": r.get("language", ""),
                        "stars_period": r.get("stars_period", 0),
                        "stars_total": r.get("stars_total", 0),
                        "score": r.get("score", 0),
                        "is_chinese": r.get("is_chinese", False),
                        "appearances": r.get("appearances"),
                        "cum_score": r.get("cum_score"),
                    }
                    for r in repos
                ],
            }, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    # ---- Tool 3: run_daily_agent_reach ---------------------------------

    def run_daily_agent_reach(
        self,
        dry_run: bool = True,
        account_ref: str = "halfking",
    ) -> str:
        """
        运行完整的每日 Agent Reach 流水线（多源抓取 → 翻译 → 发帖）。

        完整流程：
          1. 抓取 GitHub Trending (daily + weekly)
          2. 抓取 Twitter (AI GitHub 项目)
          3. 多源融合 + 评分排序 + 规则筛选
          4. GitHub API 补充信息
          5. LLM 翻译 description + README
          6. 发微博（dry_run=True 时仅生成内容，不发送）

        Args:
            dry_run: 是否仅生成内容不发送（默认 True，建议先验证）
            account_ref: 微博账号标识（默认 halfking）

        Returns:
            JSON 格式的运行结果（含生成的微博内容）

        Example:
            run_daily_agent_reach(dry_run=True)
            run_daily_agent_reach(dry_run=False)  # 真实发帖
        """
        script_path = self.SCRIPTS_DIR / "daily-x-to-weibo.py"
        if not script_path.exists():
            return json.dumps({
                "success": False,
                "error": f"script_not_found: {script_path}",
                "hint": "确保 /opt/trendaradar/agent_reach/scripts/daily-x-to-weibo.py 存在",
            }, ensure_ascii=False)

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
            tail_out = (proc.stdout or "")[-500:]

            return json.dumps({
                "success": success,
                "dry_run": dry_run,
                "account_ref": account_ref,
                "return_code": proc.returncode,
                "post": {
                    "content": post_data.get("content", ""),
                    "github_url": post_data.get("github_url", ""),
                    "hook": post_data.get("meta", {}).get("hook", ""),
                    "topics": post_data.get("meta", {}).get("topics", []),
                    "selected_repo": post_data.get("meta", {}).get("selected_name", ""),
                },
                "script_output_tail": tail_out,
                "error": proc.stderr[-200:] if proc.returncode != 0 else "",
            }, ensure_ascii=False, indent=2)

        except subprocess.TimeoutExpired:
            return json.dumps({
                "success": False,
                "error": "script_timeout_10min",
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    # ---- Tool 4: save_agent_reach_data ---------------------------------

    def save_agent_reach_data(
        self,
        title: str,
        content: str,
        url: str = "",
        keywords: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        外部智能体上报 Agent Reach 相关数据到 TrendRadar DB。

        用于：其他智能体抓取的 GitHub/AI 相关数据，统一写入 TrendRadar，
        通过 search_news_unified 供所有智能体查询。

        Args:
            title: 数据标题（如 "owner/repo" 或项目名）
            content: 正文内容（description + 摘要等）
            url: 原文链接（GitHub repo URL 等）
            keywords: 关键词列表（topics 翻译后的中文标签）
            metadata: 附加元数据（如 stars, language, source 等）

        Returns:
            JSON 格式的保存结果（含 crawl_records id）

        Example:
            save_agent_reach_data(
                title="stan-ko/meshy",
                content="MCP协议的Python实现，支持stdio和HTTP...",
                url="https://github.com/stan-ko/meshy",
                keywords=["MCP", "Python", "框架"],
                metadata={"stars": 4820, "language": "Python"},
            )
        """
        try:
            keywords_zh = keywords or []
            if keywords:
                keywords_zh = _translate_topics(keywords)

            record_id = self.storage.save_crawl_record(
                url=url,
                title=title,
                content=content,
                source_name="agent_reach",
                source_type="github",
                keywords_matched=keywords_zh,
                relevance_score=(metadata or {}).get("score", 0.0),
                metadata=metadata or {},
            )

            return json.dumps({
                "success": True,
                "crawl_record_id": record_id,
                "title": title,
                "keywords_zh": keywords_zh,
            }, ensure_ascii=False)

        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    # ---- Tool 5: search_agent_reach_news --------------------------------

    def search_agent_reach_news(
        self,
        query: str,
        limit: int = 10,
        date_range: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        从 TrendRadar DB 检索已存储的 Agent Reach / GitHub Trending 数据。

        数据来源：fetch_github_trending 和 save_agent_reach_data 写入的记录。

        Args:
            query: 搜索关键词（项目名、描述、技术栈等）
            limit: 返回条数上限（默认 10）
            date_range: 日期范围过滤，格式 {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}

        Returns:
            JSON 格式的搜索结果

        Example:
            search_agent_reach_news(query="AI Agent MCP", limit=5)
            search_agent_reach_news(query="Python", date_range={"start": "2026-06-01", "end": "2026-06-09"})
        """
        try:
            records = self.storage.search_crawl_records(
                query=query,
                source_type="github",
                keywords=[],
                start_date=date_range.get("start") if date_range else None,
                end_date=date_range.get("end") if date_range else None,
                limit=limit,
            )

            results = []
            for r in records:
                meta = r.get("metadata") or {}
                results.append({
                    "id": r["id"],
                    "title": r["title"],
                    "description": r.get("content", "")[:300],
                    "url": r.get("url", ""),
                    "keywords": r.get("keywords_matched", []),
                    "stars_period": meta.get("stars_period", 0),
                    "stars_total": meta.get("stars_total", 0),
                    "language": meta.get("language", ""),
                    "created_at": r.get("created_at", ""),
                })

            return json.dumps({
                "success": True,
                "query": query,
                "total": len(results),
                "records": results,
            }, ensure_ascii=False, indent=2)

        except Exception as e:
            return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)

    # ---- Utilities ------------------------------------------------------

    def _load_ranking(self, date_str: str) -> Dict[str, Any]:
        for dir_path in [self.DB_RANKING_DIR, self.RANKING_DIR]:
            path = dir_path / f"{date_str}.json"
            if path.exists():
                try:
                    with open(path) as f:
                        return json.load(f)
                except Exception:
                    pass
        return {}
