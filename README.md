# Qwen3.8-27B 内网多人推理服务

在 **RTX 4090** 上用 **vLLM** 部署 `Qwen3.8-27B-AWQ-INT4`，前面挂一层自研 **gateway**（OpenAI 兼容），
再配 **Open WebUI** 作为多人使用的聊天前端。核心目标：**每人独立历史、跨会话长期记忆、多人并发可控**。

```
 浏览器 ──► Open WebUI :8080 ──► gateway :9000 ──► vLLM :8000 ──► 4×RTX 4090
            (账号/登录/UI)      (身份→用户/历史/记忆  │  (仅内网)
                                 /并发闸门/限额/审计)
```

每个请求经过网关时做的事：

1. 从 `X-OpenWebUI-User-Id` 等转发头认出「是谁」；
2. 取出该用户该会话的历史 + 该用户的长期记忆，拼成真正的 prompt；
3. 按 token 预算裁剪（客户端传多少都不影响上游显存）；
4. 转发给 vLLM（流式透传，边生成边返回）；
5. 落库消息、记 token 用量、异步抽取长期记忆。

---

## 目录结构

```
llm_serving/
├── gateway/                  # OpenAI 兼容网关（FastAPI，纯 Python，无 GPU 依赖）
│   ├── api/                  # 路由：chat / models / health / personal / admin
│   ├── config.py             # pydantic-settings：yaml + 环境变量
│   ├── db.py,schema.sql      # SQLite（WAL）：users/sessions/messages/memories/usage/audit
│   ├── history.py            # 上下文装配与裁剪
│   ├── memory.py             # 长期记忆：抽取、去重、中文检索打分
│   ├── limiter.py            # 并发闸门 / RPM / 每日 token 配额
│   ├── extractor.py          # 异步记忆抽取队列
│   ├── services.py, app.py   # 依赖装配、应用工厂
│   └── __main__.py           # python -m gateway
├── configs/gateway.yaml      # 网关基线配置（可被 QIANWEN_* 环境变量覆盖）
├── scripts/
│   ├── install.sh            # 建 venv 装依赖（uv）
│   ├── start_vllm.sh         # 启动 vLLM（自动按空闲显存下调 util）
│   ├── start_gateway.sh      # 启动网关
│   ├── start_webui.sh        # 启动 Open WebUI（打开用户身份透传）
│   ├── fetch_weight.sh       # ★ 用 modelscope 把 21GB 权重下到 ./weight/
│   ├── docker_up.sh          # ★ 一键 docker 部署（权重 + 构建 + 启动）
│   ├── patch_webui_branding.sh  # 把页面署名样式装进 Open WebUI
│   ├── check_paths.sh        # 可移植性检查：禁止仓库里出现本机绝对路径
│   └── smoke_test.sh         # 端到端自检
├── docker/
│   ├── Dockerfile.vllm       # 基于官方 vllm-openai 镜像
│   ├── Dockerfile.gateway    # 轻量 python:3.12-slim
│   ├── Dockerfile.weights    # 权重下载器（modelscope，单独镜像）
│   └── scripts/              # 三个容器入口脚本
├── webui/branding/custom.css # 页面署名（作者：丁亮 ）
├── docker-compose.yml        # vllm + gateway + open-webui 一键起
├── docs/
│   ├── deployment-checklist.md  # ★ 上线前必读的注意事项清单
│   ├── architecture.md          # 架构 / 数据模型 / 记忆算法
│   ├── api.md                   # 网关接口手册（curl 示例）
│   ├── clients.md               # 接入 Claude Code / DeepSeek Harness
│   └── operations.md            # 日常运维、备份、排障
├── tests/                    # 52 个单测（pytest，无需 GPU）
├── requirements-gateway.txt  # 网关运行时依赖
├── requirements-dev.txt      # + pytest / ruff
├── ruff.toml                 # 固定 lint 规则集（换 ruff 版本不会突然报新错）
├── .env.example              # 所有可调参数
└── Makefile                  # make test / lint / up / down / logs
```

---

## 获取代码

```bash
git clone https://github.com/dingliangxiansheng/llm_serving.git
cd llm_serving
cp .env.example .env        # 首次：按需修改；Docker 一键脚本也会自动生成并随机填密钥
```

下文所有命令都假定**当前目录是仓库根目录**。仓库里不写死任何本机绝对路径，脚本用
`BASH_SOURCE` 自己定位工程根，所以 clone 到哪个目录都能跑。

---

## 快速开始（Docker 一键，推荐）

前置：NVIDIA 驱动 + `nvidia-container-toolkit` + docker compose v2。

```bash
./scripts/docker_up.sh          # 下载权重 -> 构建镜像 -> 启动 -> 等健康
```

权重是**构建/部署时自动下载**的，不需要手工准备（21 GB，用 modelscope，落在当前仓库的
`weight/Qwen3.8-27B-AWQ-INT4`）：

```bash
make weight           # 只下权重（已完整则跳过，可反复执行）
make weight-check     # 只校验 ./weight 下是否完整，不下载
./scripts/docker_up.sh --weight-only   # 只下权重，不构建不启动
./scripts/docker_up.sh --no-weight     # 权重已就位时跳过下载
```

实现方式：`docker/Dockerfile.weights` 做一个只装 modelscope 的下载器镜像，运行时把宿主机
`./weight` bind-mount 进去，所以下载结果直接落在宿主机上，21 GB 权重也不会进 vLLM 镜像。
中断后重跑会逐文件续传；下载完会校验 `config.json` / `index.json` / 每个分片大小，防止半截权重。

完成后：

- 前端：`http://<服务器IP>:8080`（管理员账号见 `.env` 的 `WEBUI_ADMIN_EMAIL/PASSWORD`）
- 网关管理接口：`curl -H "X-Admin-Token: $ADMIN_TOKEN" http://127.0.0.1:9000/admin/overview`

> 第一次启动会拉 `vllm/vllm-openai` 基础镜像（数 GB），并加载 21GB 权重（2~5 分钟），
> 可用 `docker compose logs -f vllm` 观察。

常用命令：

```bash
docker compose ps                 # 状态
docker compose logs -f gateway    # 网关日志
docker compose down               # 停止（保留数据卷）
./scripts/docker_up.sh --down     # 同上
./scripts/smoke_test.sh           # 端到端自检（会真的问模型一句）
```

---

## 快速开始（不用 Docker，直接跑在宿主机）

系统 python 是 3.10 且没有 `ensurepip`，所以这里用 `uv` 建独立环境：

```bash
git clone https://github.com/dingliangxiansheng/llm_serving.git
cd llm_serving

./scripts/install.sh              # 建 .venv-vllm(3.12) / .venv-gateway(3.12) / .venv-webui(3.11)
cp .env.example .env              # 按需修改

bash scripts/start_all.sh         # 一键：vLLM → 网关 → Open WebUI，并等到都就绪
bash scripts/smoke_test.sh        # 端到端验证（可选）
bash scripts/stop_all.sh --gpu    # 全部停止并确认显存释放
```

也可以分开三个终端各自起（调试时更方便）：

```bash
./scripts/start_vllm.sh     # 终端 1，首次约 10 分钟（图捕获 + 多模态 warmup）
./scripts/start_gateway.sh  # 终端 2
./scripts/start_webui.sh    # 终端 3，默认 8080
```

---

## 配置要点（完整项见 `.env.example` 与 `configs/gateway.yaml`）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MODEL_PATH` | `./weight/Qwen3.8-27B-AWQ-INT4` | 权重目录（相对仓库根；compose 只读挂载） |
| `MODEL_ID` | `cyankiwi/Qwen3.8-27B-AWQ-INT4` | modelscope 下载的模型仓库 |
| `TENSOR_PARALLEL_SIZE` | `4` | **2 卡装不下 21GB 权重，必须 4 卡** |
| `GPU_MEMORY_UTILIZATION` | `0.33` | 当前机器每卡被占 15GB；独占后可提到 0.9 |
| `MAX_MODEL_LEN` | `8192` | 上下文；显存够可 32768 |
| `MAX_NUM_SEQS` | `4` | vLLM 侧并发；与网关 `GATEWAY_MAX_CONCURRENT` 对齐 |
| `GATEWAY_MAX_PROMPT_TOKENS` | `6144` | **必须小于 `MAX_MODEL_LEN`** |
| `AUTH_MODE` | `header` | 多人场景只能用它（按 Open WebUI 用户区分） |
| `ADMIN_TOKEN` | 随机 | 管理接口口令，仅运维持有 |
| `WEBUI_ENABLE_SIGNUP` | `false` | 关自助注册，管理员建号 |
| `GATEWAY_MEMORY_EXTRACT_MODE` | `llm` | `heuristic` 可省一次模型调用 |

> 网关的环境变量前缀沿用早期的 `QIANWEN_`（例如 `QIANWEN_UPSTREAM__BASE_URL`）。仓库已改名为
> `llm_serving`，但这个前缀属于对外配置接口，保持不变以免老部署的 `.env`/compose 失效。

---

## 网关提供的能力

| 能力 | 位置 |
| --- | --- |
| OpenAI 兼容对话（流式/非流式） | `POST /v1/chat/completions` |
| 工具调用透传（`tools` + `tool_choice=auto`） | 网关原样转发；vLLM 侧需 `--tool-call-parser qwen3_coder` |
| 思考过程（`reasoning` 字段） | vLLM `--reasoning-parser qwen3`，Open WebUI 自动折叠展示 |
| 模型列表（给 Open WebUI 发现） | `GET /v1/models` |
| 我自己的会话 / 消息 / 记忆 / 用量 | `GET/DELETE /my/...` |
| 管理：总览、用户、用量、审计、代管记忆 | `GET /admin/...`（需 `X-Admin-Token`） |
| 探针 / Prometheus 指标 | `/healthz`、`/readyz`、`/metrics` |

细节见 [docs/api.md](docs/api.md)。

---

## 页面署名

页面右下角有一行小字 **「作者：丁亮 」**，来源是 `webui/branding/custom.css`：

```bash
make branding                  # 本机 venv 部署：装进 .venv-webui 里 Open WebUI 的 static/custom.css
bash scripts/patch_webui_branding.sh --check    # 检查是否已装（已装返回 0，未装返回 2）
bash scripts/patch_webui_branding.sh --restore  # 还原成 Open WebUI 出厂的空样式
```

- 原理：Open WebUI 的前端模板会加载 `/static/custom.css`（官方留的覆盖入口），所以用一条
  `body::after` 伪元素渲染署名，**不改前端构建产物**，也不动 Open WebUI 的品牌资源。
- 注意：Open WebUI **每次启动都会用 `open_webui/frontend/static/` 重建 `open_webui/static/`**，
  所以脚本写的是 `frontend/static/custom.css`（源头），直接写 `static/custom.css` 会被覆盖。
- Docker 部署不用手工执行：`docker-compose.yml` 已把该文件只读挂载进容器的
  `/app/backend/open_webui/frontend/static/custom.css`。
- `scripts/start_webui.sh` 与 `scripts/install.sh` 也会顺带执行一次（幂等，失败只告警）。
- 改文案或位置的唯一位置就是 `webui/branding/custom.css`，改完硬刷新浏览器即可。

---

## 上线前必读

**先看 [docs/deployment-checklist.md](docs/deployment-checklist.md)**，尤其是这几条：

1. **显存得先算清楚**：21 GB 权重 + KV Cache。独占的 2×4090 可以用 TP=2（每卡 ~10.5 GB 权重），
   本机这种「每卡已被占 15.3 GB」的情况必须 TP=4 + `GPU_MEMORY_UTILIZATION≈0.33`；
2. 必须打开 `ENABLE_FORWARD_USER_INFO_HEADERS`，否则所有人共用一个匿名用户、历史互通；
3. 网关端口不能对外暴露，否则可伪造 `X-OpenWebUI-User-Id` 冒充他人；
4. `GATEWAY_MAX_PROMPT_TOKENS` 要小于 `MAX_MODEL_LEN`，否则上游 400。

想接 **Claude Code**（终端或 VS Code 扩展）或 **DeepSeek Harness**？见
[docs/clients.md](docs/clients.md)：vLLM 原生提供 Anthropic `/v1/messages`，改几个环境变量即可。

## 实测数据（4×4090，每卡被宿主机其它容器占 15.3GB，仅剩 8.18GB）

| 项 | 实测值 |
| --- | --- |
| 权重加载 | 每卡 4.92 GiB（21GB ÷ 4），约 40s |
| KV Cache | 1.13 GiB / 卡 → 64,170 tokens（`MAX_MODEL_LEN=32768` 时可并发 1.96 路，8k 提示可并发 8 路） |
| 从启动到就绪 | 约 10 分钟（CUDA graph 捕获 + 多模态 warmup） |
| 单轮问答 | 76 token 回答 2.0s（`reasoning_effort=low`）；xhigh 时 4.9s |
| 记忆抽取 | 每轮结束异步跑一次，约 15s，不阻塞回答 |
| 30k token 长请求（Claude Code 的系统提示规模） | 预填充约 23s，输出正常 |

显存紧张时把 `--enforce-eager` 加回 `VLLM_EXTRA_ARGS`：KV Cache 能回到 1.53 GiB / 61,680 tokens，
但每 token 更慢（实测约 6 tok/s 生成）。

## 测试

```bash
.venv-gateway/bin/python -m pytest tests -q     # 52 passed，不需要 GPU
```

## 常见问题

- **vLLM 报 `Free memory on device ... less than desired GPU memory utilization`**：
  显存被别的进程占用，参考脚本自动下调 util，或先释放显存。
- **网关报 500 `FileNotFoundError: 'ninja'`**（vLLM 侧）：flashinfer 采样器需要 ninja 在 PATH 里，
  `scripts/start_vllm.sh` 已自动处理；手工启动时请用 `PATH=.venv-vllm/bin:$PATH`。
- **所有人看到同一段历史**：`ENABLE_FORWARD_USER_INFO_HEADERS` 没开，或 `AUTH_MODE` 不是 `header`。
- **上游 400 / 回答被截断**：`GATEWAY_MAX_PROMPT_TOKENS` 与 `MAX_MODEL_LEN` 不匹配。
- **记忆不生效**：`GATEWAY_MEMORY_EXTRACT_MODE=off`，或对话轮次不足（少于 `extract_min_chars`）；
  用 `GET /my/memories` 确认是否抽到。
