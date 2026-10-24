# 网关接口手册

所有接口都在网关（默认 `http://127.0.0.1:9000`）。身份来源见 [architecture.md](architecture.md#3-身份与权限)。

```bash
export GW=http://127.0.0.1:9000
export UID_HDR='X-OpenWebUI-User-Id: u-demo'
export CHAT_HDR='X-OpenWebUI-Chat-Id: c-demo'
export ADMIN_TOKEN=<你的 ADMIN_TOKEN>
```

---

## 1. OpenAI 兼容

### `POST /v1/chat/completions`

与 OpenAI 协议一致；额外支持 Open WebUI 的转发头。流式与非流式都支持。

```bash
# 非流式
curl -s $GW/v1/chat/completions \
  -H 'Content-Type: application/json' -H "$UID_HDR" -H "$CHAT_HDR" \
  -d '{"model":"qwen3.8-27b","messages":[{"role":"user","content":"你好"}],"max_tokens":128}'

# 流式
curl -N $GW/v1/chat/completions \
  -H 'Content-Type: application/json' -H "$UID_HDR" -H "$CHAT_HDR" \
  -d '{"model":"qwen3.8-27b","stream":true,"messages":[{"role":"user","content":"用三句话介绍 vLLM"}]}'
```

网关在该接口上做的事：

| 行为 | 说明 |
| --- | --- |
| 身份解析 | 缺 `X-OpenWebUI-User-Id` 时按 `auth.mode` 兜底（`shared`/`off`） |
| 会话定位 | 优先 `X-OpenWebUI-Chat-Id`；缺省用 `sha1(用户 + 首条问题)` 生成稳定会话 id |
| 历史装配 | 取该会话最近 `history.max_turns` 轮，按 `max_prompt_tokens` 裁剪 |
| 记忆注入 | 命中 ≥ `memory.inject_top_k` 条时追加【用户长期记忆】系统段 |
| 强制记账 | 流式请求自动注入 `stream_options={"include_usage": true}` |
| 落库 | 回答结束后写 `messages` + `usage_log`，并提交异步记忆抽取 |
| 请求体清洗 | 丢弃 Open WebUI 私有字段（如 `user`），其余原样透传 |

错误码：

| 状态 | `error.code` | 含义 |
| --- | --- | --- |
| 400 | — | `messages` 为空 / 请求体不是合法 JSON |
| 401 | — | 配置了 `auth.api_keys` 但未提供/不匹配 Bearer token |
| 429 | `rate_limited` | 触发单用户 RPM，带 `Retry-After` |
| 429 | `quota_exceeded` | 当日 token 配额用尽，带 `Retry-After`（到次日 UTC 0 点） |
| 502 | — | 连不上 vLLM / 上游 5xx |
| 503 | `server_busy` | 并发排队超时，带 `Retry-After` |
| 504 | — | 上游请求超时 |

### `GET /v1/models`

给前端发现模型用，返回 `settings.upstream.model` 一项：

```bash
curl -s $GW/v1/models
# {"object":"list","data":[{"id":"qwen3.8-27b","object":"model","owned_by":"llm-serving"}]}
```

---

## 2. 探针与指标

| 接口 | 说明 |
| --- | --- |
| `GET /healthz` | 网关自身存活（不探上游），永远 200 |
| `GET /readyz` | 含上游探测；vLLM 未就绪返回 503 |
| `GET /metrics` | Prometheus 文本格式 |

```bash
curl -s $GW/healthz; echo
curl -s $GW/readyz; echo
curl -s $GW/metrics | head
```

指标（`qw_` 前缀）：`qw_inflight`（当前占用槽位）、`qw_waiting`（排队中）、
`qw_extractor_queue`（记忆抽取积压）、`qw_uptime_seconds`、`qw_tokens_total{direction="prompt|completion"}`、
`qw_requests_total`、`qw_request_latency_ms_*`。

---

## 3. 用户自助 `/my/*`

| 方法与路径 | 说明 |
| --- | --- |
| `GET /my/profile` | 我的 id/邮箱/角色 + 会话数/消息数/记忆数/今日 token |
| `GET /my/sessions?limit=50` | 我的会话列表（按更新时间倒序） |
| `GET /my/sessions/{id}/messages?limit=200` | 某会话的消息；**别人的会话返回 404** |
| `DELETE /my/sessions/{id}` | 删会话（级联删消息） |
| `GET /my/memories?kind=` | 我的长期记忆（可按 kind 过滤） |
| `DELETE /my/memories/{id}` | 删一条记忆 |
| `DELETE /my/memories` | 清空我的全部记忆 |
| `GET /my/usage?days=7` | 我最近 N 天的 token/请求数 |

```bash
curl -s $GW/my/profile -H "$UID_HDR"
curl -s "$GW/my/sessions?limit=5" -H "$UID_HDR"
curl -s $GW/my/memories -H "$UID_HDR"
curl -s -X DELETE $GW/my/memories -H "$UID_HDR"
curl -s "$GW/my/usage?days=30" -H "$UID_HDR"
```

`kind` 取值：`preference` / `fact` / `project` / `profile` / `other`。

---

## 4. 管理接口 `/admin/*`

统一要求：`-H "X-Admin-Token: $ADMIN_TOKEN"`（或 `X-OpenWebUI-User-Role: admin`）。
`auth.admin_token` 为空时全部返回 503（防止无口令裸奔）。

| 方法与路径 | 说明 |
| --- | --- |
| `GET /admin/overview` | 运行总览：版本、模型、认证模式、并发、抽取队列、用户/会话/消息/记忆计数、近 7 天用量 |
| `GET /admin/users?limit=100` | 用户列表 + 每人统计 |
| `GET /admin/users/{user_id}` | 单人详情 |
| `GET /admin/users/{user_id}/memories` | 查看某人的记忆 |
| `POST /admin/users/{user_id}/memories` | 代写一条记忆（body `{"text": "...", "kind": "fact"}`，重复 → 409） |
| `DELETE /admin/users/{user_id}/memories/{memory_id}` | 删某人的一条记忆 |
| `GET /admin/sessions/{session_id}/messages?user_id=...` | 审计某会话；**必须显式传 user_id**，防止越权遍历 |
| `GET /admin/usage?days=7` | 全员每日用量 |
| `GET /admin/audit?limit=100` | 管理操作审计日志 |

```bash
curl -s $GW/admin/overview -H "X-Admin-Token: $ADMIN_TOKEN" | python -m json.tool
curl -s $GW/admin/users -H "X-Admin-Token: $ADMIN_TOKEN" | python -m json.tool
curl -s -X POST $GW/admin/users/u-demo/memories \
  -H "X-Admin-Token: $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"text":"负责 kd-gateway 后端","kind":"project"}'
curl -s "$GW/admin/usage?days=7" -H "X-Admin-Token: $ADMIN_TOKEN"
curl -s "$GW/admin/audit?limit=20" -H "X-Admin-Token: $ADMIN_TOKEN"
```

---

## 5. 前端接入（Open WebUI）

在 Open WebUI 里把网关当成一个 OpenAI 连接即可，关键是**打开用户身份透传**：

```yaml
environment:
  OPENAI_API_BASE_URLS: http://gateway:9000/v1
  OPENAI_API_KEYS: sk-llm-serving-internal
  ENABLE_FORWARD_USER_INFO_HEADERS: "true"   # 不开 → 所有人共用一个匿名用户
```

`docker-compose.yml` 与 `scripts/start_webui.sh` 都已配置好。
