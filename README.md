# Hermes 自动化脚本（Agent-Reach + 微博/Twitter + LLM 用量追踪）

> 配套 [Agent-Reach](https://github.com/Panniantong/Agent-Reach) 的 Hermes 自动化脚本集，部署在 `~/.agent-reach/` 用户配置目录下。
>
> **路由**：所有 LLM 调用走自有通道 `https://llm.kxpms.cn`（不走收费 API），用量统一写入 ACC（`https://acc.kxpms.cn`）。

## 脚本清单

| 文件 | 作用 |
|------|------|
| `scripts/daily-x-to-weibo.py` | 每日 Twitter AI 项目抓取 → 翻译改写 → 自动发微博（含图片）|
| `scripts/weibo_poster.py` | 微博 camoufox 发帖器（支持远程图片URL + 本地图片）|
| `scripts/llm_tracker.py` | LLM 调用追踪：MiniMax 调用 + ACC 用量写入（HMAC-SHA256 token）|
| `scripts/acc_sync.py` | ACC 用量查询 CLI（today/week/task/channel）|

## 安装

```bash
# 1. 克隆到 ~/.agent-reach/scripts/
mkdir -p ~/.agent-reach && cd ~/.agent-reach
git clone https://github.com/halfking/agent-reach-hermes-scripts.git .

# 2. 创建 Python venv（agent-reach 已安装）
ls ~/.agent-reach-venv/bin/python3  # 应已存在

# 3. 配置 cookie（手动）
echo 'SUB=...; SUBP=...; ...' > config/weibo_cookie.txt

# 4. 测试
~/.agent-reach-venv/bin/python3 scripts/daily-x-to-weibo.py
```

## LLM 通道

- **端点**：`https://llm.kxpms.cn/v1/chat/completions`
- **认证**：`POST /api/auth/token` → 获取 Bearer token（55 分钟缓存）
- **模型**：`minimax-m2.7`（默认）
- **定价**：input $0.05/1M tokens, output $0.20/1M tokens

## ACC 集成

每次任务自动：
1. `create_task_session()` → 在 ACC 创建 session（task_id）
2. `LLMWrapper.chat()` → 调用 LLM，捕获 usage
3. `post_usage()` → 写入 `acc.llm_usage_logs`

查询：
```bash
~/.agent-reach-venv/bin/python3 scripts/acc_sync.py today
~/.agent-reach-venv/bin/python3 scripts/acc_sync.py channel weibo
```

## Cronjob

```
0 9,13,17,21 * * *   每天 4 次（9/13/17/21时）
```

## 内容过滤规则

- GitHub stars ≥ 10K
- Twitter 推文点赞数 ≥ 10K
- 自动翻译 description + README 为中文
- 新项目（< 6 月）单独标识，不用"top1"等误导描述

## License

MIT
