# 运维手册

## 0. 获取代码与目录约定

```bash
git clone https://github.com/dingliangxiansheng/llm_serving.git
cd llm_serving
```

仓库**不含任何本机绝对路径**：所有脚本用 `BASH_SOURCE` 推导 `ROOT_DIR`，配置项（`MODEL_PATH`、
`GATEWAY_DB_PATH`、`HF_CACHE_DIR` 等）都是相对仓库根的路径，因此 clone 到 `/opt`、`~/work`、
数据盘任意目录都能直接跑。`make lint` 里的 `scripts/check_paths.sh` 会拦住手滑写死的绝对路径。

---

## 1. 启停

```bash
# Docker（推荐）
docker compose up -d                 # 启动全部
docker compose ps                    # 状态（看 healthy）
docker compose restart gateway       # 只重启网关（秒级，不重新加载模型）
docker compose down                  # 停止（数据卷保留）

# 宿主机（不用 docker）
./scripts/start_vllm.sh              # 前台，日志直接看
nohup ./scripts/start_vllm.sh > logs/vllm.log 2>&1 &
./scripts/start_gateway.sh
./scripts/start_webui.sh
```

启动顺序：vLLM（2~5 分钟，21GB 权重 + 多模态预热）→ gateway → Open WebUI。
compose 已用 `depends_on: service_healthy` 串好；手工启动时 gateway 可以先生起来（`/readyz` 会 503）。

## 2. 健康检查

```bash
curl -s http://127.0.0.1:8000/health        # vLLM
curl -s http://127.0.0.1:9000/healthz       # 网关自身
curl -s http://127.0.0.1:9000/readyz        # 含上游（503 = 上游没好）
curl -s http://127.0.0.1:8080/health        # Open WebUI
./scripts/smoke_test.sh                     # 端到端（真问一句）
```

## 3. 备份与恢复

网关库（SQLite，WAL 模式，**不要直接 cp 正在写的文件**）：

```bash
# 备份
docker exec llm-serving-gateway python - <<'PY'
import sqlite3
src = sqlite3.connect('/data/gateway.db'); dst = sqlite3.connect('/data/backup.db')
src.backup(dst); dst.close(); src.close()
PY
docker cp llm-serving-gateway:/data/backup.db ./backup-gateway-$(date +%F).db

# 恢复（先停网关）
docker compose stop gateway
docker cp ./backup-gateway-2026-10-24.db llm-serving-gateway:/data/gateway.db
docker compose start gateway
```

Open WebUI 数据（账号、会话、上传文件）在卷里：

```bash
docker run --rm -v llm-serving_webui-data:/data -v $PWD:/backup alpine \
  tar czf /backup/webui-$(date +%F).tgz -C /data .
```

建议 cron：每天 03:00 备份两个库，保留 14 天。

## 4. 清理与容量

```bash
# 看各表规模
docker exec llm-serving-gateway python - <<'PY'
import sqlite3
c = sqlite3.connect('/data/gateway.db')
for t in ('users','sessions','messages','memories','usage_log','audit_log'):
    print(t, c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0])
PY
```

```sql
-- 删除 90 天前的会话（消息级联删除）
DELETE FROM sessions WHERE updated_at < datetime('now','-90 day');
-- 删除 180 天前的用量 / 审计
DELETE FROM usage_log WHERE created_at < datetime('now','-180 day');
DELETE FROM audit_log WHERE created_at < datetime('now','-365 day');
-- 收缩文件（需要先停写）
VACUUM;
```

容器日志别无限长：`/etc/docker/daemon.json` 里配 `log-opts: {"max-size":"100m","max-file":"3"}`。

## 5. 监控

- 拉 `/metrics`（Prometheus 格式）到现有监控里，重点看：`qw_inflight`（长期打满=要加卡）、
  `qw_extractor_queue`（持续增长=模型太忙，建议 `GATEWAY_MEMORY_EXTRACT_MODE=heuristic`）、
  `qw_requests_total{status="503"}`（排队超时=闸门太紧或 GPU 不够）。
- 也可以直接看：`curl -s -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:9000/admin/overview`。
- GPU：`nvidia-smi dmon -s mu -d 5`（利用率/显存）。

## 5.1 页面署名与自定义样式

页面右下角那行「作者：丁亮 · 仅供本次演示使用」来自 `webui/branding/custom.css`，
它被复制到 Open WebUI 的 `open_webui/frontend/static/custom.css`——这是 Open WebUI 官方留的样式入口
（`/static/custom.css`），不需要改前端构建产物，也不需要重新打镜像。

> 为什么不是 `open_webui/static/custom.css`：Open WebUI 启动时会清空 `static/` 并从
> `frontend/static/` 重新复制（`open_webui/config.py` 的 "Static DIR" 段），写在那里会被覆盖掉。

```bash
make branding                                    # 本机 venv：装进 .venv-webui
bash scripts/patch_webui_branding.sh --check     # 已装返回 0，未装返回 2
bash scripts/patch_webui_branding.sh --restore   # 还原成出厂空样式
bash scripts/patch_webui_branding.sh --target /path/to/open_webui/static/custom.css
```

- Docker 部署：`docker-compose.yml` 已把该文件只读挂载到
  `/app/backend/open_webui/frontend/static/custom.css`，改完 `docker compose up -d open-webui` 即可。
- `scripts/install.sh` 与 `scripts/start_webui.sh` 会顺带执行（幂等，失败只告警）。
- 生效方式：浏览器**硬刷新**（Ctrl/Cmd+Shift+R）；用户侧无需重新登录。
- 只改文案/位置就改这一个 CSS 文件；不要动 Open WebUI 的品牌资源（`static/BRANDING.md` 有许可说明）。
- 若升级后镜像里的包路径变了（mount 目标不存在会让容器起不来），先用
  `docker compose exec open-webui python -c "import open_webui,os;print(os.path.dirname(open_webui.__file__))"`
  确认实际路径，再改 `docker-compose.yml` 里的挂载目标。

## 6. 升级

```bash
# 1) 备份数据库（见上）
# 2) 更新代码/镜像 tag
# 3) 重建并滚动重启
docker compose up -d --build
./scripts/smoke_test.sh
```

只改网关代码时：`docker compose up -d --build gateway`（vLLM 不动，不重新加载 21GB 权重）。
升级 vLLM 大版本前先在测试环境验证——AWQ INT4 走 Marlin 内核，换版本可能换内核实现。

回滚：镜像都带 tag（`IMAGE_TAG`），`docker compose up -d` 指回旧 tag；数据库用备份覆盖。

## 7. 排障速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| vLLM 启动报 `Free memory on device ... less than desired` | 显存被占，util 设太高 | 降 `GPU_MEMORY_UTILIZATION`；`nvidia-smi` 看谁在占 |
| vLLM OOM / 反复重启 | `MAX_MODEL_LEN` 太大或并发太高 | 先降长度到 8192，再降 `MAX_NUM_SEQS` |
| 请求返回 500，日志 `FileNotFoundError: 'ninja'` | flashinfer 采样器 JIT 需要 ninja 在 PATH | 用 `scripts/start_vllm.sh`（已处理）或 `PATH=.venv-vllm/bin:$PATH` |
| 所有人看到同一段历史 | `ENABLE_FORWARD_USER_INFO_HEADERS` 未开 / `AUTH_MODE != header` | 改 Open WebUI 环境变量并重启 |
| 网关 401 | 配了 `auth.api_keys` 但前端 key 不匹配 | 对齐 `GATEWAY_API_KEY` / `OPENAI_API_KEYS` |
| 网关 429 `rate_limited` | 单用户 RPM 限流 | 调 `GATEWAY_RPM_PER_USER` |
| 网关 503 `server_busy` | 并发排队超时 | 调大 `GATEWAY_MAX_CONCURRENT`（不超过 vLLM `MAX_NUM_SEQS`）或加卡 |
| 上游 400 `maximum context length` | `GATEWAY_MAX_PROMPT_TOKENS` > `MAX_MODEL_LEN` | 调小到 `MAX_MODEL_LEN - 1024` 以下 |
| 回答里带出别人的信息 | 网关端口被外部访问、头被伪造 | 网关只绑 127.0.0.1，加防火墙 |
| 报 `"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser` | 请求带 tools，但 vLLM 没开工具解析 | 加 `--enable-auto-tool-choice --tool-call-parser qwen3_coder` 后重启 vLLM |
| 工具调用参数是空的/乱码 | `--tool-call-parser` 选错（不是本模型模板格式） | 本模型用 `qwen3_coder` |
| `.env` 启动报 `qwen3: command not found` | `VLLM_EXTRA_ARGS` 值含空格却没用引号 | 写成 `VLLM_EXTRA_ARGS='--reasoning-parser qwen3'` |
| 正文里混着 `</think>` 和思考文本 | 没配 `--reasoning-parser qwen3` | 加上该参数；正文/思考会分开 |
| 首个请求卡 2 分钟才回 | flashinfer / CUDA graph 首次 JIT 与图捕获 | 正常；预热一次即可（`curl` 发个 "hi"） |
| 回答很慢（>20 tok/s 之外的慢） | `reasoning_effort` 默认 xhigh，思考太长 | `--default-chat-template-kwargs '{"reasoning_effort":"low"}'` |
| 首轮 10 分钟才 ready | CUDA graph 捕获 + 多模态 warmup | 正常；healthcheck `start_period: 900s` 已考虑 |
| 记忆一直为空 | `GATEWAY_MEMORY_EXTRACT_MODE=off`，或对话太短 | `curl /my/memories` 确认；调 `extract_min_chars` |
| Open WebUI 登录后没有模型 | 网关 `/v1/models` 不通 / key 不对 | `curl` 一下 `/v1/models`，看 compose 日志 |
| 页面 502 | WebUI 起来了但网关没起 | `docker compose ps`，看 gateway 健康状态 |

## 8. 日常巡检清单（每周）

- [ ] `docker compose ps` 三个容器都 healthy，重启次数没有增加
- [ ] `/admin/overview` 里 `inflight` 峰值、近 7 天用量是否异常
- [ ] `qw_extractor_queue` 没有持续积压
- [ ] 磁盘：`df -h`、数据库大小、日志大小
- [ ] 抽查 2 条用户记忆是否合理（`/admin/users/{id}/memories`）
- [ ] 备份文件确实存在且能打开（`sqlite3 backup.db 'PRAGMA integrity_check'`）
