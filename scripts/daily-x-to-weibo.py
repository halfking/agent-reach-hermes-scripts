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
    wrapper = LLMWrapper(task_id=task_id, provider="evol", model="minimax-m2.7")

    system_prompt = (
        "You are a professional tech translator. Translate the following English text to Chinese (simplified). "
        "Keep the tone engaging and suitable for a Chinese developer audience. "
        "Preserve line breaks with \\n. Do NOT add quotes or explanations."
    )

    try:
        result = wrapper.chat(
            user_message=f"Translate to Chinese:\n\n{text[:3000]}",
            max_tokens=max_tokens,
            temperature=0.7,
            metadata={"function": "translate_to_chinese", "text_len": len(text)}
        )
        content = result["content"]
        usage = result["usage"]
        log(f"✅ 翻译完成 ({len(content)} chars, {usage['total_tokens']} tokens, ${usage['cost_usd']:.6f})")
        return content.strip()
    except Exception as e:
        log(f"⚠️ 翻译失败: {e}，使用原文")
        return text


def fetch_github_readme(owner, repo):
    """通过 GitHub API 获取 README 内容（前 2000 字）"""
    try:
        api_url = f"https://api.github.com/repos/{owner}/{repo}/readme"
        req = urllib.request.Request(api_url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/vnd.github.v3+json",
        })
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
        return content[:2000].strip()
    except Exception as e:
        return ""


def fetch_github_repo_info(owner, repo):
    """通过 GitHub API 获取仓库基本信息，并翻译 description + README 为中文"""
    try:
        api_url = f"https://api.github.com/repos/{owner}/{repo}"
        req = urllib.request.Request(api_url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/vnd.github.v3+json",
        })
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
            "topics": data.get("topics", []),
            "stars": data.get("stargazers_count", 0),
            "forks": data.get("forks_count", 0),
            "language": data.get("language", ""),
            "license": data.get("license", {}).get("spdx_id", ""),
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

    log("=== Daily Twitter AI Project → Weibo v3 ===")
    
    # Step 1: 搜索 Twitter 上的 AI GitHub 项目
    log("Searching Twitter for AI GitHub projects with 10k+ stars...")
    queries = [
        "AI GitHub stars:10000",
        "AI framework open source stars:10000",
        "AI tool github stars:10000",
    ]
    tweets = []
    for q in queries:
        tweets = search_twitter(q, limit=10)
        if tweets:
            log(f"Query '{q}': got {len(tweets)} results")
            break
    
    if not tweets:
        log("❌ No Twitter results found")
        sys.exit(1)
    
    # Step 2: 筛选有 GitHub stars >= 10K 且推文点赞数 >= 10K 的项目
    MIN_TWEET_LIKES = 10000
    MIN_GITHUB_STARS = 10000
    selected = None
    for tweet in tweets:
        text = tweet.get("text", "")
        url = extract_github_url(text)
        tweet_likes = tweet.get("likeCount", 0)
        if url and has_github_stars(text, MIN_GITHUB_STARS):
            # 推文点赞数必须 >= 10K
            if tweet_likes < MIN_TWEET_LIKES:
                log(f"⏭️  跳过（GitHub stars OK，但推文点赞 {tweet_likes} < {MIN_TWEET_LIKES}）: {url}")
                continue
            real_url = expand_shortlink(url)
            selected = {
                "text": text,
                "github_url": real_url,
                "author": tweet.get("author", {}).get("screenName", ""),
                "tweet_id": tweet.get("id", ""),
                "tweet_obj": tweet,
                "engagement": {
                    "likes": tweet_likes,
                    "retweets": tweet.get("retweetCount", 0),
                    "replies": tweet.get("replyCount", 0),
                }
            }
            log(f"✅ 选中（推文点赞 {tweet_likes}）: {real_url}")
            break
    
    if not selected:
        log(f"❌ No suitable project found (GitHub stars >= {MIN_GITHUB_STARS} AND 推文点赞 >= {MIN_TWEET_LIKES})")
        sys.exit(1)
    
    # Step 3: 抓取 GitHub 详情
    gh_info = {}
    gh_url = selected["github_url"]
    if "github.com/" in gh_url:
        parts = gh_url.rstrip("/").replace("https://github.com/", "").split("/")
        if len(parts) >= 2:
            owner, repo = parts[0], parts[1]
            log(f"Fetching GitHub info: {owner}/{repo}...")
            gh_info = fetch_github_repo_info(owner, repo)
            log(f"   Stars: {gh_info.get('stars', 'N/A')}")
            log(f"   Description: {gh_info.get('description', 'N/A')}")
            log(f"   Topics: {gh_info.get('topics', [])[:5]}")
            log(f"   README length: {len(gh_info.get('readme', ''))} chars")
    
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
