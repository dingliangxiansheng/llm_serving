# CLAUDE.md

给后续维护者（含 AI 编码助手）的仓库约定。**改代码前先读这一页。**

## 这个仓库是什么

在 4×RTX 4090 上用 vLLM 部署 `Qwen3.8-27B-AWQ-INT4`，前面挂一个自研 FastAPI 网关
（OpenAI 兼容），后面接 Open WebUI 给内部多人使用。网关负责：用户隔离、会话历史、
长期记忆、并发闸门、配额、审计、指标。

代码仓库：`git clone https://github.com/dingliangxiansheng/llm_serving.git && cd llm_serving`。
仓库**不写死本机绝对路径**（脚本用 `BASH_SOURCE` 推导 `ROOT_DIR`，配置项全用相对路径），
`make lint` 会跑 `scripts/check_paths.sh` 拦住手滑写死的路径。

## 三件不要动的事

1. **不要引入 LiteLLM 之类的现成网关替代 `gateway/`**。历史/记忆/限额/审计是自研的，
   现成方案覆盖不到「按 Open WebUI 用户隔离 + 长期记忆 + 中文检索」。
2. **不要用向量库或 SQLite FTS5 做记忆检索**。FTS5 trigram 对两字中文查询失效
   （`match '后端'` 返回空），向量库要额外占 GPU。当前「单字 + 二元组 + 英文整词」的 IDF
   覆盖度打分在 `gateway/memory.py`，几百条记忆毫秒级。
3. **不要把 `Services` 或路由之间的 import 放回 `app.py`**。`Services` 必须留在
   `gateway/services.py`，否则 `app → api → app` 循环导入（历史真实故障）。

## 目录约定

```
gateway/            业务代码（无 GPU 依赖，纯 CPU 可跑测试）
  api/              路由；每个文件一个资源域
  config.py         pydantic-settings：yaml 基线 + QIANWEN_* 环境变量覆盖
  schema.sql        建表语句（改表要同时改这里和 db.py 的读写方法）
tests/              纯 CPU 单测，FakeUpstream 用 httpx.MockTransport 打桩
scripts/            宿主机运行脚本（start_*，fetch_weight.sh，patch_webui_branding.sh，smoke_test.sh，check_paths.sh）
webui/branding/     站点署名样式（custom.css，通过 /static/custom.css 覆盖生效）
docker/             Dockerfile + 容器入口脚本
configs/            网关照进进镜像的基线 yaml
docs/               清单/架构/接口/运维四份文档
```

## 常用命令

```bash
make test                                   # 52 个单测，不需要 GPU
make lint                                   # ruff check + format --check + check_paths.sh
./scripts/install.sh                        # uv 建三个 venv
./scripts/start_vllm.sh                     # 会按空闲显存自动下调 util
./scripts/smoke_test.sh                     # 端到端（真调模型）
./scripts/docker_up.sh                      # 一键 docker
```

## 代码风格

- Python 3.12，`from __future__ import annotations` 一律写在文件头。
- 注释与文档用中文，**写「为什么」而不是「做了什么」**（例如为什么不能用 FTS5）。
- 路由层只做参数/协议转换，业务逻辑放模块级函数（`history.py` / `memory.py` / `limiter.py`）。
- 日志用模块级 `log = logging.getLogger(__name__)`，不要在库里 print。
- 时间一律 `utcnow_iso()` 存 UTC；展示层再转时区。

## 必须遵守的不变量

| 不变量 | 原因 |
| --- | --- |
| 槽位必须在返回 `StreamingResponse` **之前** acquire，在 stream 的 finally + BackgroundTask 里 release，且 release 幂等 | 否则客户端中途断开就漏槽位，最终所有请求 503 |
| 流式请求必须注入 `stream_options={"include_usage": true}` | 否则拿不到 token 数，配额与统计全错 |
| 落库/记忆抽取失败只能记日志，不能影响回答 | 网关是旁路增强，不能变成单点故障 |
| 跨用户读会话返回 **404**（不是 403） | 避免探测「别人的会话是否存在」 |
| `X-OpenWebUI-User-Id` 是信任输入 | 所以网关端口只能绑 127.0.0.1，绝不能对外 |
| 任何写入 `messages`/`memories`/`sessions` 前先 `db.ensure_user()` | 这几张表对 `users(id)` 有外键，否则 `IntegrityError: FOREIGN KEY constraint failed` |
| `history.max_prompt_tokens` 必须 < vLLM `--max-model-len` | 否则上游 400（漏检会在线上表现为「偶尔报错」） |
| 工具调用轮次（正文为空、只有 `tool_calls`）要落成 `[调用工具] 名称(参数)` 标记，且不喂记忆抽取 | 否则 `/my/sessions` 会少掉一问一答，记忆里还会混进无意义的工具名 |
| vLLM 必须带 `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder` | 缺 reasoning parser 正文混入 `</think>`；缺 tool parser 则带 `tools` 的请求直接报错 |
| 仓库里不得出现宿主机绝对路径（数据盘、家目录那种以 `/` 开头的路径）；脚本用 `BASH_SOURCE` 推导 `ROOT_DIR`，配置项用相对路径 | 工程要求 clone 到任意目录都能跑，写死路径在别人机器上必炸；`make lint` 已含 `scripts/check_paths.sh` |
| 部署文档统一以 `git clone https://github.com/dingliangxiansheng/llm_serving.git` + `cd llm_serving` 开头 | 读者拿到的是仓库，不是你的机器 |

## 加功能的落点

| 想改什么 | 改哪里 |
| --- | --- |
| 新增一个 OpenAI 兼容端点 | `gateway/api/` 新文件 + `app.py` `include_router` |
| 新的限额维度（比如按部门） | `gateway/limiter.py` + `api/deps.py` 的用户解析 |
| 记忆抽取策略（换模型、定期回顾） | `gateway/extractor.py` + `gateway/prompts.py` |
| 数据库表结构 | `gateway/schema.sql` + `gateway/db.py`；老库要写迁移（目前靠 `CREATE TABLE IF NOT EXISTS`，加列需手工 `ALTER`） |
| 前端体验 | 优先改 Open WebUI 的 Admin 设置，而不是 fork 前端 |
| 页面署名/自定义样式 | 改 `webui/branding/custom.css`（唯一入口；不要改 Open WebUI 的品牌资源） |
| 镜像/部署 | `docker/` + `docker-compose.yml`，新参数同步到 `.env.example` 与 docs |

## 改了要同步的地方

`.env.example` ←→ `configs/gateway.yaml` ←→ `gateway/config.py` ←→ `docker-compose.yml` ←→ `README.md` 的配置表 ←→ `docs/deployment-checklist.md`。
加一个配置项这六处都要过一遍，否则用户会遇到「文档里有、实际不生效」。

## 已验证的事实（别重复踩）

- 21GB INT4 权重：TP=4 时每卡 `Model loading took 4.92 GiB`；TP=2 每卡要 >10GB，24GB 卡余量很小。
- 本机当前每卡被宿主机其它容器占用 15313 MiB，只剩 ~8.2 GiB：util 0.33 / max-len 8192 / max-num-seqs 4 可跑，
  KV cache 约 1.53 GiB（61680 tokens，8192 上下文约 7.5 倍并发）。
- flashinfer 采样器首次运行要 `ninja` 在 PATH 里，否则每个请求 500（`FileNotFoundError: 'ninja'`）。
- Open WebUI 0.12.x：`WEBUI_ADMIN_EMAIL/PASSWORD/NAME` 可在首次启动时创建管理员；`DEFAULT_MODELS` 可预选模型；
  `ENABLE_FORWARD_USER_INFO_HEADERS` 默认 **false**。
- Open WebUI 的 `frontend/index.html` 用 `<link rel="stylesheet" href="/static/custom.css">`，
  但 **`open_webui/static/` 每次启动都会被 `open_webui/frontend/static/` 全量覆盖**
  （`open_webui/config.py` 的 "Static DIR" 段是先 unlink 再 copy）。所以自定义样式必须写
  `frontend/static/custom.css`（署名就是这么做的），写 `static/custom.css` 会在下次启动被清空——
  这个坑踩过，别再踩。
- 仓库改名过：目录原叫 `code/qianwen`，现为 `code/llm_serving`；但 `QIANWEN_*` 环境变量前缀
  与 `gateway/` 包名保持不变（对外接口，老 `.env` 仍可用）。
