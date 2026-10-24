# 架构与实现说明

## 1. 组件与职责

| 组件 | 技术 | 职责 | 是否占 GPU |
| --- | --- | --- | --- |
| vLLM | vLLM 0.31.0 + compressed-tensors INT4 | 只做推理，OpenAI 兼容 API（`/v1/chat/completions`） | 是 |
| gateway | FastAPI + SQLite | 身份识别、历史装配、记忆注入、并发闸门、配额、审计、指标 | 否 |
| Open WebUI | open-webui 0.12.x | 登录、账号、聊天界面、把用户身份放进请求头 | 否 |
| 站点样式 | `webui/branding/custom.css` | 页面署名；装到 `open_webui/frontend/static/custom.css`，启动时被复制到 `/static/custom.css`，不改前端产物 | 否 |

分层理由：vLLM 是无状态的、只认 prompt；「谁是谁、记住什么、能不能用」属于业务，必须放在它前面。
把业务塞进 Open WebUI 的插件里也能做，但那样每换一次前端就得重写一遍，且无法审计。

## 2. 请求链路（流式）

```
Open WebUI
  │ POST /v1/chat/completions   + X-OpenWebUI-User-Id / -Chat-Id / -User-Role
  ▼
gateway/api/chat.py
  ├─ require_user()          身份 → CurrentUser(id,email,name,role,session_id)
  ├─ 配额检查                 usage: 今日 token ≥ quota → 429 quota_exceeded
  ├─ limiter.acquire(user)   滑动 60s RPM → 并发 semaphore（超时 → 503 server_busy）
  ├─ history.recent()        取该用户该会话最近 N 轮（DB）
  ├─ memory.search()         在自己的记忆里检索 → 命中则拼接【用户长期记忆】系统段
  ├─ build_context()         system 合并 + 轮次裁剪 + token 预算裁剪
  ├─ upstream.stream_chat()  转发（强制 stream_options.include_usage）
  ├─ StreamingResponse       SSE 原样透传，顺手喂给 SSEAccumulator 解析增量
  └─ finally()               落库 assistant 消息 + log_usage + metrics + extractor.submit()
```

要点：

- **槽位在返回 `StreamingResponse` 之前获取**，释放挂在 stream 的 `finally` 与
  `BackgroundTask` 上（`_Slot.release` 幂等），客户端中途断开也会还槽位。
- 客户端断开 → `asyncio.CancelledError` → 记录 `status=499` 后重新抛出，不吞异常。
- `stream_options={"include_usage": true}` 由网关强制注入，否则拿不到 token 数，用量和配额都不准。
- 流式解析用「空行分隔事件」的增量缓冲（`gateway/sse.py`），跨 chunk 的半截 JSON 不会被误判。

## 3. 身份与权限

`auth.mode`：

| 模式 | 用户来源 | 用途 |
| --- | --- | --- |
| `header`（默认） | `X-OpenWebUI-User-Id` 等转发头 | 多人内网，唯一推荐 |
| `shared` | 固定 `default_user_id` | 只有一个公共账号时 |
| `off` | 同上，且视为 admin | 本机调试 |

- 头名可用 `auth.header_user_id` 等字段改名（对应 Open WebUI 的 `FORWARD_*` 环境变量）。
- `require_admin`：既认 `X-Admin-Token`（与 `auth.admin_token` 比对），也认
  `X-OpenWebUI-User-Role: admin`；`admin_token` 为空时管理接口直接 503（避免裸奔）。
- 跨用户读取一律 404（不是 403），避免探测「别人的会话是否存在」。

## 4. 数据模型（`gateway/schema.sql`）

| 表 | 关键字段 | 说明 |
| --- | --- | --- |
| `users` | id, email, name, role | 由网关于首次请求时 upsert；`Database.ensure_user()` 兜底 |
| `sessions` | id = Open WebUI chat id | 一个对话一行 |
| `messages` | session_id, role, content, tokens, content_hash | 全量历史；**唯一索引 `(session_id, role, content_hash)`** 做重发去重 |
| `memories` | user_id, text, norm, kind, weight, hits | `UNIQUE(user_id, norm)` 做完全重复归并 |
| `usage_log` | user_id, prompt_tokens, completion_tokens, latency_ms, status | 用量与错误率 |
| `audit_log` | user_id, action, detail | 管理操作留痕 |

设计取舍：

- 单文件 SQLite + WAL + `busy_timeout=30s`，20~200 人内网足够；换成 Postgres 只需替换 `gateway/db.py`。
- 连接是**单条 + RLock**（`check_same_thread=False`）：网关的写都是小事务，串行化反而省去迁移成本。
- 历史与记忆表对 `users(id)` 有外键 → 写入前统一 `ensure_user()`，避免「先有聊天记录后有用户行」失败。

## 5. 上下文装配（`gateway/history.py`）

```
build_context(messages, max_turns=20, max_prompt_tokens=6144):
    1. 只保留最近 max_turns*2 条（一进一出算一轮）
    2. 保证 system 在最前（没有就补一个默认 system）
    3. 从最旧开始丢，直到估算 token ≤ 预算；最后一条（当前问题）永远保留
```

- token 估算不引模型分词器：CJK 每字 0.7 + 其它每字符 0.3 + 每条消息 4（宁可高估）。
- **历史有两个来源**：客户端（Open WebUI 每次带整段会话）+ 网关库。以网关裁剪为准；
  落库时用 `content_hash` 去重，所以重发同一段历史不会把 DB 撑大。

## 6. 长期记忆

写入（异步，不阻塞回答）：

```
同一轮结束后 → extractor.submit(ExtractionJob)
  → 队列（maxsize=256，满则丢弃并计 dropped）
  → extract_mode=llm  : 让小模型输出 JSON [{"text","kind"}]（parse 宽容）
    extract_mode=heuristic : 正则匹配「我是/我喜欢/我的项目是…」
  → MemoryStore.add(): 归一化 → 完全重复则 weight+0.5 → Jaccard≥0.9 视为近似重复跳过
    → 每人上限 max_per_user=500，超出按 weight/时间淘汰
```

读取（同步，回答前）：

```
memory.search(user_id, query, top_k=5, min_score=0.12)
  切词：英文/数字整词 + 中文单字 + 相邻二元组
  切句：先按 。！？；换行 把提问切成若干句，每句单独算覆盖度，取最大值
        （用户一句话问多件事时，整句分母会把只对上一件事的短记忆稀释到阈值以下）
  打分：覆盖率 × (0.75 + 0.25×新鲜度) × min(weight, 3)
       覆盖率 = max over 句 ( 句内命中词 IDF 之和 / 该句全部词 IDF 之和 )
       新鲜度 = 0.5 ** (age_days / half_life_days=180)
  → render_memory_block() 拼成【用户长期记忆】系统段（受 max_inject_tokens=600 限制）
  → mark_used() 累加 hits 与 last_used_at
```

实测：同一句「我叫什么名字？我负责哪个后端服务？」下，两条记忆得分 0.130 / 0.489，
按整句只算一次时名字那条会被压到 0.12 阈值以下（模型就会答"不知道你的名字"）。

为什么不用向量库 / FTS5：

- FTS5 的 trigram 分词对**两字中文查询**失效（实测 `match '后端服'` 有结果、`match '后端'` 返回空），
  而中文口语里两字词是主力；
- 向量库要额外装 embedding 模型并占 GPU/内存，对「几百条记忆、内网 20 人」是过度设计。
- 当前方案是纯 Python，检索耗时在毫秒级，且零额外依赖。

## 7. 并发与限额

| 机制 | 参数 | 行为 |
| --- | --- | --- |
| 并发闸门 | `limits.max_concurrent=4` | `asyncio.Semaphore`，排队超 `queue_timeout_s` → 503 + `Retry-After` |
| 单用户 RPM | `limits.rpm_per_user=60` | 60 秒滑动窗口 → 429 + `Retry-After` |
| 每日配额 | `limits.daily_token_quota=0` | 0 表示不限；超限 → 429 `quota_exceeded` |
| 上游超时 | `upstream.request_timeout_s=600` | 超时 → 504；连接失败 → 502 |

## 7.1 思考（reasoning）内容

模型是推理型（chat_template 自带 `<think>`）。vLLM 侧用 `--reasoning-parser qwen3` 把思考过程
拆到响应的顶层 `reasoning` 字段（非流式在 `message.reasoning`，流式在 `delta.reasoning`），
网关**不解析也不改写**，原样透传。

- Open WebUI 0.12.3 的 `utils/middleware.py:3453` 会读 `reasoning_content` / `reasoning` / `thinking`
  三者之一并渲染成可折叠的思考块，所以前端无需改造。
- `--default-chat-template-kwargs '{"reasoning_effort":"low"}'` 把思考长度从默认 xhigh 降下来：
  实测同一问题 76 token 的回答从 4.9s 降到 2.0s。
- 思考 token 同样计入 usage（`completion_tokens_details.reasoning_tokens`），预算按总量算。

闸门数应 ≤ vLLM `--max-num-seqs`，让排队发生在网关（能返回有意义的状态码），而不是在上游。

## 8. 失败与降级

| 场景 | 行为 |
| --- | --- |
| 上游 4xx | 原样透传 body 与状态码，仍记 usage(status=4xx) |
| 上游 5xx / 连不上 | 502，`{"error":{"message":...}}` |
| 记忆抽取失败 | 只记日志，降级 heuristic，绝不影响回答 |
| 记忆抽取积压 | 队列满即丢弃（`dropped` 计数进 `/metrics`） |
| 落库失败 | 记日志，回答照常返回 |
| 网关自身异常 | 全局 handler → 500，不泄露堆栈细节 |

## 9. 模块依赖

```
app.py → services.py → {config, db, history, memory, limiter, upstream, extractor, metrics}
   │
   └→ api/{chat,models,personal,admin,health} → api/deps.py → services
```

`Services` 放在 `gateway/services.py` 而不是 `app.py`，是为了打断 `app → api → app` 的循环导入
（曾经真实报错：`ImportError: cannot import name 'Services' from partially initialized module 'gateway.app'`）。
