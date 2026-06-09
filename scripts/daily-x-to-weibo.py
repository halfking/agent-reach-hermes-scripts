#!/usr/bin/env python3
"""
每天定时任务 v3：搜 Twitter AI 项目 → 深度抓取 GitHub 详情 → 生成高吸引力微博
- 搜索条件：AI + GitHub 项目 + stars >= 10k
- 从推文提取：文字 + 所有图片
- 从 GitHub 抓取：README 内容 + 仓库截图
- 生成高吸引力中文 hook + 完整介绍 + 图片
- 通过本地 camoufox 发送（v2 真发）
"""
import os
import re
import sys
import json
import time
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

# 导入 LLM 追踪模块
sys.path.insert(0, str(Path.home() / ".agent-reach" / "scripts"))
from llm_tracker import create_task_session, LLMWrapper, print_usage_report, query_usage_summary

COOKIE_FILE = Path.home() / ".agent-reach" / "config" / "weibo_cookie.txt"
STORAGE_STATE = "/tmp/weibo-halfking-storage-state.json"
ACCOUNT_REF = "halfking"
OUTPUT_JSON = "/tmp/daily-weibo-post.json"
GITHUB_IMG_CACHE = "/tmp/github_repo_imgs/"

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def search_twitter(query, limit=5):
    """用 twitter-cli 搜索，返回 JSON"""
    cmd = ["twitter", "search", query, "-n", str(limit), "--json"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
        return data.get("data", [])
    except:
        return []

def parse_stars(text):
    """从文本中提取 GitHub stars 数量，支持中文和英文格式"""
    import re
    # Patterns (priority order)
    patterns = [
        r'(\d+\.?\d*)\s*万\s*(?:stars?|★)',  # 1.5万 stars, 2万stars
        r'(\d+\.?\d*[kK])\s*(?:stars?|★)',   # 12.3k stars
        r'stars?\s*[:=]?\s*(\d+\.?\d*[kK])', # stars: 12.3k
        r'(\d{1,3}(?:,\d{3})+)',             # 10,000
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            val = m.group(1).replace(",", "")
            if '万' in p and 'k' not in val.lower():
                return float(val) * 10000
            if 'k' in val.lower():
                return float(val.lower().replace('k','')) * 1000
            return int(val)
    return 0

def has_github_stars(text, min_stars=10000):
    """检测文本中是否有 >= min_stars 的 GitHub 项目"""
    stars = parse_stars(text)
    return stars >= min_stars

def extract_github_url(text):
    """提取 GitHub URL（支持 t.co 短链）"""
    import re
    # 先找直接的 github.com URL
    m = re.search(r'https?://github\.com/[a-zA-Z0-9_./-]+', text)
    if m:
        return m.group(0).rstrip(',.。;:）)')
    # t.co 短链也算 GitHub（用于 Twitter 推文）
    m2 = re.search(r'https?://t\.co/[a-zA-Z0-9]+', text)
    if m2:
        return m2.group(0)
    return None


def extract_images_from_tweet(tweet):
    """从推文数据中提取图片 URL 列表"""
    media = tweet.get("media", [])
    images = []
    for m in media:
        if m.get("type") == "photo":
            url = m.get("url", "")
            if url:
                images.append(url)
    return images


def expand_shortlink(url):
    """展开短链，获取真实 URL（用于 t.co 短链）"""
    if not url.startswith("https://t.co/"):
        return url
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        req.get_method = lambda: 'HEAD'
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.url
    except Exception:
        return url


def translate_to_chinese(text, max_tokens=800):
    """
    用 MiniMax (LLM-Gateway 自有通道) 把英文翻译成中文
    返回翻译后的文本，失败时返回原文本
    """
    # 创建 task-scoped wrapper（复用同一个 task_id）
    task_id = os.environ.get("ACC_TASK_ID", f"weibo-daily-{datetime.now().strftime('%Y-%m-%d-%H')}")

    system_prompt = (
        "你是专业的科技翻译。把用户提供的英文文本翻译成简体中文，目标读者是中国开发者。"
        "要求：(1) 准确传达技术含义；(2) 语言地道流畅；(3) 保留 GitHub/AI/LLM 等技术术语；"
        "(4) 不要添加引号、解释或'翻译：'前缀；(5) 直接输出翻译结果，不要重复原文。"
    )

    wrapper = LLMWrapper(task_id=task_id, provider="evol", model="minimax-m2.7", system_prompt=system_prompt)

    try:
        result = wrapper.chat(
            user_message=f"请把下面的英文翻译成中文（直接输出中文，不要任何前缀）：\n\n{text[:3000]}",
            max_tokens=max_tokens,
            temperature=0.7,
            metadata={"function": "translate_to_chinese", "text_len": len(text)}
        )
        content = result["content"]
        usage = result["usage"]
        # 检测翻译失败：返回过短 或 仍无中文
        if len(content.strip()) < 5 or not any("\u4e00" <= ch <= "\u9fff" for ch in content):
            log(f"⚠️ 翻译可疑（{len(content)}chars，无中文）: {content[:50]}，使用原文")
            return text
        log(f"✅ 翻译完成 ({len(content)} chars, {usage['total_tokens']} tokens, ${usage['cost_usd']:.6f})")
        return content.strip()
    except Exception as e:
        log(f"⚠️ 翻译失败: {e}，使用原文")
        return text


def translate_topics(topics: list) -> list:
    """把 GitHub topics 翻译/映射成中文标签（用本地字典优先，少量未知再 LLM）。"""
    # 高频技术 topics 本地映射（避免每次都调 LLM）
    TOPIC_MAP = {
        "ai": "AI", "ml": "机器学习", "deep-learning": "深度学习",
        "machine-learning": "机器学习", "llm": "大模型", "agent": "智能体",
        "agentic": "智能体", "rag": "RAG", "chatbot": "聊天机器人",
        "openai": "OpenAI", "anthropic": "Anthropic", "claude": "Claude",
        "claude-code": "Claude Code", "chatgpt": "ChatGPT", "gpt": "GPT",
        "framework": "框架", "library": "库", "tool": "工具", "cli": "命令行",
        "api": "API", "sdk": "SDK", "rest": "REST", "graphql": "GraphQL",
        "browser": "浏览器", "automation": "自动化", "scraping": "爬虫",
        "stealth": "反检测", "fingerprint": "指纹",
        "python": "Python", "javascript": "JavaScript", "typescript": "TypeScript",
        "rust": "Rust", "go": "Go", "golang": "Go", "java": "Java",
        "frontend": "前端", "backend": "后端", "fullstack": "全栈",
        "react": "React", "vue": "Vue", "nextjs": "Next.js",
        "nodejs": "Node.js", "fastapi": "FastAPI", "django": "Django",
        "database": "数据库", "postgres": "PostgreSQL", "mysql": "MySQL",
        "redis": "Redis", "mongodb": "MongoDB", "vector-database": "向量数据库",
        "search": "搜索", "vector-search": "向量搜索", "embedding": "嵌入",
        "image": "图像", "video": "视频", "audio": "音频", "voice": "语音",
        "diffusion": "扩散模型", "stable-diffusion": "Stable Diffusion",
        "computer-vision": "计算机视觉", "nlp": "自然语言处理",
        "open-source": "开源", "self-hosted": "自部署",
        "docker": "Docker", "kubernetes": "K8s", "cloud": "云",
        "security": "安全", "privacy": "隐私", "encryption": "加密",
        "compression": "压缩", "performance": "性能", "optimization": "优化",
        "monitoring": "监控", "logging": "日志", "metrics": "指标",
        "blockchain": "区块链", "web3": "Web3", "crypto": "加密货币",
        "game": "游戏", "gamedev": "游戏开发",
        "education": "教育", "tutorial": "教程", "awesome": "精选",
        "mcp": "MCP", "prompt-engineering": "提示工程", "fine-tuning": "微调",
        "tokens": "Tokens", "token-optimization": "Token 优化",
        "context-window": "上下文窗口", "context-engineering": "上下文工程",
        "proxy": "代理", "gateway": "网关", "load-balancer": "负载均衡",
    }
    result = []
    for t in topics[:8]:
        t_lower = t.lower().strip()
        result.append(TOPIC_MAP.get(t_lower, t))  # 没映射就保留原文
    return result


def _github_headers():
    """构造 GitHub API 请求头，自动注入 token 提升 rate limit。

    优先级: GITHUB_TOKEN / GH_TOKEN 环境变量 -> `gh auth token` -> 无认证。
    无认证: 60 req/h, 认证后: 5000 req/h.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 agent-reach-daily-x-to-weibo",
        "Accept": "application/vnd.github.v3+json",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        try:
            import subprocess
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                token = result.stdout.strip()
        except Exception:
            pass
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_github_readme(owner, repo):
    """通过 GitHub API 获取 README 内容（前 2000 字）"""
    try:
        api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
        req = urllib.request.Request(api_url, headers=_github_headers())
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        import base64
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        # 检测是否为 HTML（GitHub API 返回的可能是 HTML 或 Markdown）
        is_html = "<" in content and ("<p" in content or "<img" in content or "<div" in content)
        if is_html:
            # 用简易方式剥离 HTML 标签
            content = re.sub(r'<[^>]+>', '', content)
        else:
            # 清洗 Markdown 语法，保留纯文本
            content = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', content)
            content = re.sub(r'[#`*_~\[\]]', '', content)
        content = re.sub(r'\n{3,}', '\n\n', content)
        # 过滤 ASCII art / box drawing 字符（项目 logo 在 README 头部，对中文读者无用）
        content = re.sub(r'[\u2500-\u259F\u2580-\u259F█▀▄▌▐│─┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬═║╲╱╳]+', '', content)
        # 删除只剩空白/标点的行
        lines = [ln for ln in content.split('\n') if ln.strip() and len(re.sub(r'\W', '', ln)) > 2]
        content = '\n'.join(lines)
        content = re.sub(r'\n{3,}', '\n\n', content)
        return content[:2000].strip()
    except Exception as e:
        return ""


def fetch_github_repo_info(owner, repo):
    """通过 GitHub API 获取仓库基本信息，并翻译 description + README 为中文"""
    try:
        api_url = f"https://api.github.com/repos/{owner}/{repo}"
        req = urllib.request.Request(api_url, headers=_github_headers())
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        raw_desc = data.get("description", "")
        raw_readme = fetch_github_readme(owner, repo)

        # 翻译 description 和 README
        log("🌏 翻译 description + README 为中文...")
        cn_desc = translate_to_chinese(raw_desc, max_tokens=200) if raw_desc else ""
        cn_readme = translate_to_chinese(raw_readme, max_tokens=600) if raw_readme else ""

        # 判断是否为新项目（创建时间 < 6个月）
        created_at_str = data.get("created_at", "")
        is_new_project = False
        if created_at_str:
            try:
                from datetime import datetime
                created_at = datetime.strptime(created_at_str[:10], "%Y-%m-%d")
                age_days = (datetime.now() - created_at).days
                is_new_project = age_days < 180  # 6个月内算新项目
                log(f"   仓库创建时间: {created_at_str[:10]}（{age_days}天），{'新项目' if is_new_project else '老项目'}")
            except Exception:
                pass

        return {
            "description": cn_desc or raw_desc,  # 优先中文
            "description_en": raw_desc,           # 保留英文原文
            "homepage": data.get("homepage", ""),
            "topics": translate_topics(data.get("topics", [])),  # 翻译/映射成中文标签
            "topics_en": data.get("topics", []),
            "stars": data.get("stargazers_count", 0),
            "forks": data.get("forks_count", 0),
            "language": data.get("language", ""),
            "license": data.get("license", {}).get("spdx_id", "") if data.get("license") else "",
            "open_issues": data.get("open_issues_count", 0),
            "subscribers": data.get("subscribers_count", 0),
            "readme": cn_readme or raw_readme,     # 优先中文
            "readme_en": raw_readme,               # 保留英文原文
            "is_new": is_new_project,
        }
    except Exception as e:
        return {}


def download_github_screenshots(repo_url, cache_dir, max_imgs=3):
    """抓取 GitHub 仓库页面截图（用 camoufox）"""
    os.makedirs(cache_dir, exist_ok=True)
    saved = []
    
    # 尝试用 camoufox 截图 README 中的图片
    try:
        from camoufox import Camoufox
        with Camoufox(headless=True) as browser:
            page = browser.new_page()
            # 先去仓库主页截图
            page.goto(repo_url, wait_until="networkidle", timeout=20000)
            page.screenshot(
                path=f"{cache_dir}/repo_main.png",
                full_page=False
            )
            saved.append(f"{cache_dir}/repo_main.png")
            
            # 尝试截取 README 区域（如果有的话）
            try:
                readme_el = page.query_selector("#readme, .markdown-body, .BorderGrid-cell")
                if readme_el:
                    readme_el.screenshot(path=f"{cache_dir}/readme.png")
                    saved.append(f"{cache_dir}/readme.png")
            except:
                pass
            
            page.close()
    except Exception as e:
        log(f"⚠️ 截图失败: {e}")
    
    return saved


def collect_all_images(tweet, github_info, repo_url):
    """
    收集所有图片：推文图片（远程URL）+ GitHub 截图（本地路径）
    返回 (all_urls, local_paths) 元组，供 weibo_poster 分开处理
    """
    remote_urls = []
    local_paths = []
    
    # 1. 推文图片（远程 URL）
    tweet_imgs = extract_images_from_tweet(tweet)
    remote_urls.extend(tweet_imgs)
    
    # 2. GitHub 截图（本地路径）
    if repo_url:
        cache_dir = GITHUB_IMG_CACHE
        gh_imgs = download_github_screenshots(repo_url, cache_dir)
        local_paths.extend(gh_imgs)
    
    return remote_urls, local_paths  # 分开返回，weibo_poster 分别处理

def rewrite_for_weibo_v3(tweet_text, github_url, github_info, tweet_engagement=None, is_new=False):
    """
    v3 重写：基于 GitHub 详情生成高吸引力微博
    结构：
      【一句话 hook】
      核心亮点（从 README/description 提炼）
      技术/场景标签
      GitHub 链接
    总计不超过 500 字（微博支持长文）
    """
    import random
    
    # 解析 GitHub URL
    parts = github_url.rstrip("/").replace("https://github.com/", "").split("/")
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    repo_name = repo.replace("-", " ").replace("_", " ")
    
    desc = github_info.get("description", "")
    readme = github_info.get("readme", "")
    desc_en = github_info.get("description_en", desc)
    readme_en = github_info.get("readme_en", readme)
    topics = github_info.get("topics", [])
    stars = github_info.get("stars", 0)
    forks = github_info.get("forks", 0)
    language = github_info.get("language", "")

    # 格式化数字
    def fmt_num(n):
        if n >= 10000:
            return f"{n/10000:.1f}万"
        if n >= 1000:
            return f"{n/1000:.1f}k"
        return str(n)

    stars_str = fmt_num(stars)
    forks_str = fmt_num(forks)

    # 从中文 README 第一段提取核心功能描述
    readme_snippet = ""
    if readme:
        paras = readme.split("\n\n")
        for p in paras:
            p = p.strip()
            if len(p) > 30 and len(p) < 300:
                readme_snippet = p
                break

    # 如果没有 README，用 description
    if not readme_snippet and desc:
        readme_snippet = desc

    # === 生成 Hook（用英文版做关键词匹配）===
    check_text = (desc_en + " " + readme_en).lower()
    
    # 根据话题选择最贴切的 hook（用英文原文匹配关键词）
    if any(t in check_text for t in ["browser", "stealth", "fingerprint", "anti-detect"]):
        hook = f"反检测浏览器新项目！{stars_str} Stars"
    elif any(t in check_text for t in ["agent", "agentic"]):
        hook = f"AI Agent 基础设施开源！{stars_str} Stars"
    elif any(t in check_text for t in ["code", "coding", "programming", "lint", "format"]):
        hook = f"程序员必备的 {stars_str} 开源工具！{language or '代码'}"
    elif any(t in check_text for t in ["image", "video", "生成", "diffusion", "stable"]):
        hook = f"AI 生成效果炸裂！{stars_str} Stars — 开源免费"
    elif any(t in check_text for t in ["api", "gateway", "proxy"]):
        hook = f"API 网关开源！{stars_str} Stars — 后端开发必备"
    elif any(t in check_text for t in ["database", "db", "sql", "postgres", "mysql"]):
        hook = f"数据库工具开源！{stars_str} Stars"
    elif is_new:
        hook = f"GitHub 新项目：{repo_name}，{stars_str} Stars"
    else:
        hook = f"GitHub {stars_str} Stars 开源项目：{repo_name}"

    # === 核心亮点（从 README 提炼）===
    highlights = []
    
    if readme_snippet:
        # 清洗并截取精华句
        snippet = re.sub(r'\s+', ' ', readme_snippet)
        if len(snippet) > 150:
            # 找第一个句号或换行截断
            m = re.search(r'[。.!?\n]', snippet[:150])
            if m:
                snippet = snippet[:m.end()]
            else:
                snippet = snippet[:147] + "..."
        highlights.append(snippet)
    
    if desc and desc != readme_snippet:
        highlights.append(desc)
    
    # 如果有 topics，加上标签
    if topics:
        tag_str = " · ".join(topics[:4])
        highlights.append(f"🏷️ {tag_str}")
    
    # === 构建最终内容 ===
    lines = [hook, ""]
    for h in highlights:
        lines.append(h)
    lines.append("")
    lines.append(f"⭐ {stars_str} Stars  · 🍴 {forks_str} Forks")
    lines.append(f"🔗 {github_url}")
    
    content = "\n".join(lines)
    
    # 如果总字数超过 500，压缩
    if len(content) > 500:
        # 优先保留 hook + 链接，截断亮点
        hook_line = hook
        link_line = f"🔗 {github_url}"
        stars_line = f"⭐ {stars_str} Stars  · 🍴 {forks_str} Forks"
        available = 500 - len(hook_line) - len(link_line) - len(stars_line) - 10
        body_lines = [h for h in highlights if h.startswith("🏷️")]
        text_lines = [h for h in highlights if not h.startswith("🏷️")]
        combined = "\n".join(text_lines)
        if len(combined) > available:
            combined = combined[:available-3] + "..."
        body_lines.insert(0, combined)
        content = "\n".join([hook_line, "", "\n".join(body_lines), "", stars_line, link_line])
    
    return content, {
        "repo": repo_name,
        "owner": owner,
        "stars": stars_str,
        "forks": forks_str,
        "language": language,
        "topics": topics,
        "hook": hook,
        "highlights": highlights,
    }


def save_storage_state(cookie_str, storage_path):
    """将 cookie 字符串转为 Playwright storage_state JSON 并保存"""
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k or not v:
            continue
        cookies.append({
            "name": k,
            "value": v,
            "domain": ".weibo.com",
            "path": "/"
        })
    
    state = {"cookies": cookies, "origins": []}
    with open(storage_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    log(f"Storage state saved: {storage_path}")

def sync_and_login_weibo():
    """同步 cookie 并通过 weibo_login_run 注入登录态"""
    if not os.path.exists(COOKIE_FILE):
        log(f"Cookie file not found: {COOKIE_FILE}")
        return False

    with open(COOKIE_FILE) as f:
        cookie_raw = f.read().strip()

    # 调用 trendradar.weibo_login_run 直接注入 cookie
    import urllib.parse
    encoded_cookie = urllib.parse.quote(cookie_raw)
    cmd = [
        "mcporter", "call", "trendradar.weibo_login_run",
        f"account_ref={ACCOUNT_REF}",
        f"cookie_raw={encoded_cookie}"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    log(f"weibo_login_run result (code={result.returncode}): {result.stdout[:300]}")
    return result.returncode == 0

def call_weibo_post_via_mcp(content):
    """通过 mcporter 调用 trendradar MCP 的 weibo_post"""
    import urllib.parse

    # URL encode content to handle special chars
    encoded_content = urllib.parse.quote(content)
    
    # 正确格式: mcporter call trendradar.weibo_post key=value
    dry_run = "true"
    cmd = [
        "mcporter", "call", "trendradar.weibo_post",
        f"account_ref={ACCOUNT_REF}",
        f"content={encoded_content}",
        f"dry_run={dry_run}"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return result.stdout, result.returncode

if __name__ == "__main__":
    # 生成 task_id 并创建 ACC session
    run_id = datetime.now().strftime("%Y-%m-%d-%H")
    task_id = f"weibo-daily-{run_id}"
    os.environ["ACC_TASK_ID"] = task_id
    create_task_session(
        channel="weibo",
        run_id=run_id,
        description="Twitter AI 项目 → 微博转发 v3（翻译+截图+发帖）"
    )

    log("=== Daily Multi-Source AI Project → Weibo v4 ===")
    log("数据源: GitHub Trending + Twitter | 中文市场优先 | 多规则筛选")

    # ========================================================================
    # Step 1: 多源候选采集
    # ========================================================================
    candidates = []

    # 源 1: GitHub Trending（daily + weekly + 中文）
    log("📡 Source 1: GitHub Trending (daily/weekly/chinese)...")
    try:
        sys.path.insert(0, str(Path.home() / ".agent-reach" / "scripts"))
        from github_trending import fetch_trending_multi, score_repo, is_chinese_text, save_ranking

        # daily：当日爆款
        daily = fetch_trending_multi(since="daily")
        # weekly：本周持续热门
        weekly = fetch_trending_multi(since="weekly")

        for r in daily:
            candidates.append({
                "source": "github-trending-daily",
                "github_url": r["url"],
                "owner": r["owner"],
                "repo": r["repo"],
                "description": r["description"],
                "language": r["language"],
                "stars_period": r["stars_period"],  # today 增量
                "stars_total": r["stars_total"],
                "is_chinese": is_chinese_text(r["description"]),
                "score": r["score"],
                "tweet": None,
            })
        for r in weekly:
            # weekly 热门补充
            candidates.append({
                "source": "github-trending-weekly",
                "github_url": r["url"],
                "owner": r["owner"],
                "repo": r["repo"],
                "description": r["description"],
                "language": r["language"],
                "stars_period": r["stars_period"],  # this week 增量
                "stars_total": r["stars_total"],
                "is_chinese": is_chinese_text(r["description"]),
                "score": r["score"] * 0.7,  # weekly 权重略低于 daily
                "tweet": None,
            })
        log(f"   ✅ Trending: daily={len(daily)}, weekly={len(weekly)}")

        # 持久化每日榜单
        ranking_path = save_ranking(daily[:20])
        log(f"   💾 每日 Top 20 榜单已保存: {ranking_path}")
    except Exception as e:
        log(f"   ⚠️ GitHub Trending 抓取失败: {e}")

    # 源 2: Twitter 搜索（保留老逻辑作为补充源）
    log("📡 Source 2: Twitter (AI GitHub keywords)...")
    queries = [
        "AI GitHub stars:1000",
        "open source AI tool",
        "github.com agent AI",
    ]
    tweets = []
    for q in queries:
        try:
            tweets = search_twitter(q, limit=20)
            if tweets:
                log(f"   Query '{q}': got {len(tweets)} results")
                break
        except Exception as e:
            log(f"   Query '{q}' 失败: {e}")

    for tweet in tweets:
        text = tweet.get("text", "")
        url = extract_github_url(text)
        if not url:
            continue
        real_url = expand_shortlink(url)
        if "github.com/" not in real_url:
            continue
        parts = real_url.rstrip("/").replace("https://github.com/", "").split("/")
        if len(parts) < 2:
            continue
        owner, repo = parts[0], parts[1]
        tweet_likes = tweet.get("likeCount", 0)
        candidates.append({
            "source": "twitter",
            "github_url": real_url,
            "owner": owner,
            "repo": repo,
            "description": text[:200],
            "language": "",
            "stars_period": 0,
            "stars_total": parse_stars(text),
            "is_chinese": is_chinese_text(text),
            "score": tweet_likes * 0.5 + (3000 if is_chinese_text(text) else 0),
            "tweet": tweet,
            "tweet_likes": tweet_likes,
        })
    log(f"   ✅ Twitter: {len([c for c in candidates if c['source']=='twitter'])} 个候选")

    # ========================================================================
    # Step 2: 去重 + 筛选规则（OR 条件，满足其一即可）
    # ========================================================================
    # 去重（按 owner/repo 合并，保留最高分）
    seen = {}
    for c in candidates:
        key = f"{c['owner']}/{c['repo']}"
        if key not in seen or c["score"] > seen[key]["score"]:
            seen[key] = c
    candidates = sorted(seen.values(), key=lambda x: x["score"], reverse=True)
    log(f"📊 去重后候选: {len(candidates)} 个")

    # 筛选规则（满足其一即可入选 — 中英文同等门槛，靠翻译保证中文输出）
    def passes_filter(c) -> tuple[bool, str]:
        period = c.get("stars_period", 0)
        total = c.get("stars_total", 0)
        tweet_likes = c.get("tweet_likes", 0)
        is_zh = c.get("is_chinese", False)
        source = c["source"]

        # 规则 1: GitHub Trending 当日 stars 增量 >= 300（爆款新项目，中英文同等）
        if "trending-daily" in source and period >= 300:
            tag = "🇨🇳 " if is_zh else ""
            return True, f"{tag}trending-daily +{period}/day"
        # 规则 2: GitHub Trending 周增量 >= 800（中英文同等）
        if "trending-weekly" in source and period >= 800:
            tag = "🇨🇳 " if is_zh else ""
            return True, f"{tag}trending-weekly +{period}/week"
        # 规则 3: Twitter 推文点赞 >= 5K（任何语言）
        if source == "twitter" and tweet_likes >= 5000:
            return True, f"twitter-hot likes={tweet_likes}"
        # 规则 4: Twitter 中文新爆款（中文社区讨论度）
        if source == "twitter" and tweet_likes >= 500 and is_zh:
            return True, f"twitter-chinese likes={tweet_likes}"
        return False, ""

    selected = None
    gh_info = {}
    for c in candidates:
        passed, reason = passes_filter(c)
        if not passed:
            continue
        zh_mark = "🇨🇳 " if c["is_chinese"] else ""
        log(f"🎯 候选 [{reason}]: {zh_mark}{c['owner']}/{c['repo']} (score={c['score']:.0f})")

        # 立即验证：抓取 GitHub 详情，如果失败就跳到下一个候选
        log(f"   Fetching GitHub info: {c['owner']}/{c['repo']}...")
        info = fetch_github_repo_info(c['owner'], c['repo'])
        if not info or info.get("stars", 0) == 0:
            log(f"   ⚠️ GitHub API 失败或仓库无 stars，跳过")
            continue

        log(f"   ✅ Stars: {info.get('stars')}, Description: {info.get('description', '')[:80]}")
        gh_info = info
        selected = {
            "text": c["description"],
            "github_url": c["github_url"],
            "author": c["owner"],
            "tweet_id": "",
            "tweet_obj": c.get("tweet") or {},
            "engagement": {
                "likes": c.get("tweet_likes", 0),
                "retweets": (c.get("tweet") or {}).get("retweetCount", 0),
                "replies": (c.get("tweet") or {}).get("replyCount", 0),
                "stars_period": c.get("stars_period", 0),
                "source": c["source"],
                "filter_reason": reason,
                "score": c["score"],
            },
            "is_chinese_market": c["is_chinese"],
        }
        break

    if not selected:
        log(f"❌ 无符合筛选规则的候选（候选数: {len(candidates)}）")
        log("筛选规则: trending+500/d | trending-weekly+1000/w | 中文trending+100 | twitter 10K+10K | 中文twitter 1K")
        sys.exit(1)
    
    # Step 3: GitHub 详情已在选择循环中获取（gh_info）
    gh_url = selected["github_url"]
    if gh_info:
        log(f"📦 GitHub: stars={gh_info.get('stars')}, topics={gh_info.get('topics', [])[:5]}, readme={len(gh_info.get('readme', ''))}chars")
    
    # Step 4: 收集所有图片（推文URL + GitHub本地截图）
    log("Collecting images...")
    remote_imgs, local_imgs = collect_all_images(selected["tweet_obj"], gh_info, gh_url)
    log(f"   Remote images: {len(remote_imgs)}")
    log(f"   Local images: {len(local_imgs)}")
    
    # Step 5: v3 改写（基于 GitHub README + Tweet 内容）
    is_new_project = gh_info.get("is_new", False)
    result = rewrite_for_weibo_v3(selected["text"], selected["github_url"], gh_info, selected["engagement"], is_new=is_new_project)
    if not result:
        log("❌ rewrite_for_weibo_v3 failed")
        sys.exit(1)
    
    weibo_content, meta = result
    log(f"📝 Hook: {meta['hook']}")
    log(f"📝 Content ({len(weibo_content)} chars):")
    for line in weibo_content.split("\n"):
        if line.strip():
            log(f"   {line[:80]}")
    
    # Step 6: 保存结果到 JSON
    result_data = {
        "content": weibo_content,
        "meta": meta,
        "github_url": selected["github_url"],
        "tweet_text": selected["text"],
        "images": {"remote": remote_imgs, "local": local_imgs},
        "author": selected["author"],
        "engagement": selected["engagement"],
        "selected_at": datetime.now().isoformat(),
    }
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(result_data, f, ensure_ascii=False, indent=2)
    log(f"✅ 结果已保存: {OUTPUT_JSON}")
    
    # Step 7: 用 camoufox 发微博（含推文图片 + GitHub 截图）
    log("Posting to Weibo via camoufox (v3, with images)...")
    try:
        sys.path.insert(0, str(Path.home() / ".agent-reach" / "scripts"))
        from weibo_poster import post_weibo
        
        success = post_weibo(
            content=weibo_content,
            headless=False,
            image_urls=remote_imgs if remote_imgs else None,
            local_image_paths=local_imgs if local_imgs else None,
        )
        if success:
            log("✅ 微博发帖成功！")
        else:
            log("⚠️ 微博发帖未成功（见截图）")
    except Exception as e:
        log(f"⚠️ 发帖失败: {e}")
        import traceback
        traceback.print_exc()

    # 打印本次 LLM 使用量报告
    today = datetime.now().strftime("%Y-%m-%d")
    summary = query_usage_summary(start_date=today, end_date=today, group_by="date")
    print_usage_report(summary)

    log("=== Done ===")
