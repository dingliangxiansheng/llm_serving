# 部署注意事项清单（务必先读完再上线）

> 面向：内部多人使用 · 双卡/四卡 RTX 4090 · Qwen3.8-27B-AWQ-INT4 · vLLM + Open WebUI
> 每一项都标注了「怎么判断有问题」和「怎么处理」，避免上线后踩坑。
>
> 部署前先取代码：`git clone https://github.com/dingliangxiansheng/llm_serving.git && cd llm_serving`（下文命令都在仓库根执行）。

---

## 1. 显存：这台机器最大的风险点

| 事实 | 数值 |
| --- | --- |
| 模型权重（INT4 量化后） | 21.0 GB（21017689808 字节，5 个分片） |
| 单卡 4090 显存 | 24 GB（24564 MiB） |
| **双卡可用显存** | **约 17.4 GB（每卡 8.7 GB）——装不下 21 GB 权重** |

- **结论：独占的两张 4090 能跑，但要压上下文；这台机器上（每卡被占 15.3 GB）跑不了。** 张量并行（TP）会把权重切到每张卡上：
  - TP=2（独占 24 GB 卡）：每卡 ~10.5 GB 权重，剩余 ~13 GB 给 KV Cache 与激活 —— 4k~8k 上下文、`max-num-seqs 4` 可行，`GPU_MEMORY_UTILIZATION` 可给 0.85~0.9；再长就得降；
  - TP=4：每卡权重 ~5 GB，是当前最稳的方案；
  - 4090 没有 NVLink，TP 通信走 PCIe，TP 越大每 token 延迟越高——所以「能跑」不等于「好用」。
- **当前这台机器的额外问题**：4 张卡每张都被宿主机其它容器占了 15.3 GB，只剩 **8.2 GB/卡**，必须 `TENSOR_PARALLEL_SIZE=4` + `GPU_MEMORY_UTILIZATION≈0.33` + `MAX_MODEL_LEN=8192` 才能起来（实测权重每卡 4.92 GiB）。正式对外服务请先协调独占 GPU。
- 判断方法：`nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv`，以及启动日志里是否出现 `Free memory on device ... less than desired GPU memory utilization`。
- 处理顺序（OOM 时）：降 `MAX_MODEL_LEN` → 降 `MAX_NUM_SEQS` → 降 `GPU_MEMORY_UTILIZATION` → 换 48 GB 卡 / 加卡。
- 其他：4×4090 满载约 1.4 kW，确认电源余量与机箱风道；显存被别的进程占用时 `--enforce-eager` 更省显存（代价是吞吐下降）。

## 2. 并发、吞吐与用户预期

- 单卡 24 GB 上的 27B INT4，实际并发能力有限：`--max-num-seqs 4~8` 是安全区。**人多不是问题，同时提问才是问题。**
- 网关侧有并发闸门（`limits.max_concurrent`，默认 4）：请求排队超过 `queue_timeout_s` 直接返回 503 + `Retry-After`，避免用户在前端干等。**网关的并发数要 ≤ vLLM 的 `max-num-seqs`**。
- 一定让前端走**流式**（Open WebUI 默认流式）：非流式在长回答时会撞客户端/网关超时。
- Open WebUI 自己在后台会额外调用模型：自动生成标题、生成标签、生成关注点、搜索摘要等。多人场景下这些隐性调用会抢 GPU 且计入 token 用量——**建议在 Admin 设置里关掉「自动生成标题/标签」，或给它们单独限额**。
- 给用户的预期管理：写清「当前上下文长度 8k / 模型可能幻觉 / 不要上传涉密文件」。

## 3. 身份透传：多人隔离的生死线

- Open WebUI 默认**不**转发登录用户信息（`ENABLE_FORWARD_USER_INFO_HEADERS` 默认 `false`）。不开这个开关，网关看到的所有请求都来自同一个匿名用户 → **所有人的历史、记忆会串成一个**。
  - 本项目已在 `docker-compose.yml` / `scripts/start_webui.sh` 里打开。
  - 验证：登录两个账号各问一句，然后 `curl -H 'X-OpenWebUI-User-Id: <uid>' http://127.0.0.1:9000/my/sessions`，或在管理接口 `/admin/users` 看是否出现两个人的记录。
- 网关的 `auth.mode=header` **信任** `X-OpenWebUI-User-Id` 头。因此**网关端口（9000）绝对不能暴露到办公网/公网**，否则任何人伪造请求头就能冒充别人、读取别人的记忆。
  - compose 已把网关和 vLLM 都绑在 `127.0.0.1`，只有 Open WebUI（8080）对外。
- 关闭自助注册（`WEBUI_ENABLE_SIGNUP=false`），账号由管理员创建；否则内网任何人都能注册进来占用 GPU。
- 离职/外包人员离开时：删 Open WebUI 账号 **还要** 清理网关侧数据（`users/sessions/messages/memories`），否则历史仍在库里、被后续同名账号看到的风险（按 id 隔离，实际不会串，但数据留存有合规问题）。

## 4. 上下文长度与「历史」的叠加

- 三个长度必须对齐，否则线上表现是「偶尔 400 或答非所问」：
  1. vLLM `--max-model-len`（显存预算）；
  2. 网关 `history.max_prompt_tokens`（发给模型的提示上限，默认 24576，**必须小于 max-model-len 减去回答预留**；本工程 `.env` 给 WebUI 用 6144，把余量留给并发）；
  3. Open WebUI 的「上下文消息数」设置（它自己也会截断）。
- Open WebUI 每次请求会带上**整段会话历史**，而网关同时也在库里记历史并做裁剪。两者叠加时以网关的裁剪为准（`history.max_turns`，默认 20 轮）。
- 网关对「同一会话重复发送的历史」做了去重（`uq_messages_dedup` 唯一索引），重发不会把历史记两遍。
- 长会话的表现：超出预算后最早的消息会被丢弃，模型「忘掉」开头——这是设计行为，不是 bug；需要长期记住的内容靠「长期记忆」而不是上下文。
- **给 Claude Code / DSH 这类 agent 客户端单独留余量**：它们的系统提示 + 工具定义本身就 20k tokens 左右，
  `MAX_MODEL_LEN` 设 32768 才够跑（本工程默认已是 32768，`MAX_NUM_SEQS` 相应降到 2）。详见 [clients.md](clients.md)。

## 5. 长期记忆（这版实现的特有注意事项）

- 记忆从每轮对话里异步抽取（`memory.extract_mode=llm` 默认），**每轮多一次模型调用**，会让 GPU 利用率和延迟上升。人多时改成 `heuristic`（正则兜底）或 `off`。
- 抽取是「尽力而为」：抽取失败只降级不报错，所以**不要把记忆当成数据库**，它是辅助。
- 记忆是「用户私有」的：检索只在该用户自己的记忆里做（`/my/memories` 可自助查看/删除）。
- **管理员能看到所有人的记忆**（`/admin/users/{id}/memories`）——制度上要告知用户，这属于个人信息处理。
- 记忆可能记错（模型抽取出来的偏好未必正确）：有 `min_score`、去重（`dedup_similarity`）、权重、每人上限（`max_per_user=500`）兜底，但仍建议每月抽查一次。
- 记忆检索不用向量库：中文两字词用 SQLite FTS5 trigram 会查不到（实测 `match '后端'` 返回空），所以用「单字 + 相邻二元组 + 英文整词」的 IDF 覆盖度打分，纯 Python，零额外依赖、零 GPU 占用。

## 6. 多模态（这个模型是 VL）

- 该权重是 `Qwen3_5ForConditionalGeneration`（图像 + 文本），启动时 vLLM 会分配 **encoder cache（16384 tokens）**并做多模态预热——**比纯文本模型多吃显存、启动慢约 30~60 秒**。
- 用户上传图片会让显存出现峰值，显存本就紧张时要限制图片数量/分辨率，或在前端关掉图片上传。
- 语音/文件上传（RAG）会额外拉 embedding 模型，首次会下载权重（`HF_HUB_OFFLINE=1` 时直接失败）——用不到就在 Admin 里关掉 RAG 和语音。

## 6.1 工具调用（Open WebUI 的联网搜索 / 代码解释器 / 知识库）

- Open WebUI 一旦启用「原生函数调用（Native Function Calling）」，请求就会带上 `tools` +
  `tool_choice=auto`。vLLM 默认拒绝这种请求，报：
  `"auto" tool choice requires --enable-auto-tool-choice and --tool-call-parser to be set`
  —— 用户看到的就是一句报错、拿不到回答。
- 必须给 vLLM 加两个参数（本工程 `.env` 已默认带上）：
  ```
  --enable-auto-tool-choice --tool-call-parser qwen3_coder
  ```
  `qwen3_coder` 是因为这个模型的 chat_template 用的是 `<tool_call><function=...><parameter=...>` 这套 Qwen3 格式；
  选错 parser 不会报错，但工具参数会解析成空/乱码。
- 这个模型本身**不联网**：`get_weather` 之类工具只是「生成一个调用意图」，真正执行工具的是 Open WebUI
  （需要它自己配好联网搜索/代码执行后端）。想让模型联网，要单独给 Open WebUI 配 SearXNG 之类，不在本工程范围内。
- 不想用工具就简单：在 Open WebUI 的 Admin → 模型设置里关掉原生函数调用，请求里就不会带 `tools`。
- 工具调用轮次的正文是空的，网关会把这一轮记成 `[调用工具] 函数名(参数)` 落进历史，
  否则 `/my/sessions` 会少掉一问一答；这类轮次不参与长期记忆抽取（标记文本没有语义价值）。

## 7. 数据、备份与留存

- 网关数据在 SQLite 单文件（默认 `data/gateway.db`，容器内卷 `gateway-data`），WAL 模式，适合内网 < 200 人。
- **备份**（不要直接 `cp` 正在写 WAL 的库）：
  ```bash
  docker exec llm-serving-gateway python - <<'PY'
  import sqlite3; src = sqlite3.connect('/data/gateway.db'); dst = sqlite3.connect('/data/backup.db')
  src.backup(dst); dst.close(); src.close()
  PY
  docker cp llm-serving-gateway:/data/backup.db ./backup-gateway-$(date +%F).db
  ```
- Open WebUI 的数据在卷 `webui-data`（`/app/backend/data`）：账号、会话、上传文件都在这里，同样要备份。
- 磁盘增长：`messages` 表按 token 估算，20 人 × 每天 50 轮 ≈ 几年后几 GB，但要定期 `VACUUM` 并清理 90 天前的会话（`docs/operations.md` 有 SQL）。
- 日志里可能包含用户问题（vLLM INFO 级别通常只打统计，但异常栈可能带 prompt）：日志目录定期清理，别长期裸存。

## 8. 安全与合规

- 内网也要**口令强度 + 关闭公网**；需要外网访问时前置 nginx + HTTPS + IP 白名单或 VPN，不要直接把 8080 映射到公网。
- `ADMIN_TOKEN` 只给运维；管理接口的所有写操作都会进 `audit_log`。
- 模型许可：Qwen 系列为 Apache-2.0，量化版（cyankiwi）继承上游许可；**商用前请自行确认许可条款**。内部使用也要注意数据合规：不允许上传涉密、个人敏感信息。
- 依赖供应链：镜像里 vLLM 固定小版本（`VLLM_VERSION`），网关依赖用下限约束；生产建议 `pip freeze` 锁死（`requirements-lock.txt`）。
- 提示注入：同一用户的会话/记忆是他自己的，注入只影响本人；但**管理员写入的记忆**会长期生效，写入前要确认内容。
- 演示/试用场景建议在页面上保留用途标识：本仓库默认在右下角显示「作者：丁亮 · 仅供本次演示使用」
  （`webui/branding/custom.css`）。正式上线前把它改成公司/部门标识，或用 `--restore` 去掉。

## 9. 可观测与运维

- 三个探针：`/healthz`（网关自身）、`/readyz`（含上游，503 表示还没好）、`/metrics`（Prometheus）。
- 关注指标：`qw_inflight`（当前并发）、`qw_extractor_queue`（记忆抽取积压）、`qw_tokens_total`、429/503 比例、P95 首 token 延迟。
- 日常命令：`docker compose logs -f vllm`（加载/显存问题）、`docker compose logs -f gateway`（谁在报错）。
- 升级顺序：备份数据库 → 拉新镜像 → `docker compose up -d --build`；vLLM 大版本升级可能换内核（AWQ Marlin），先在测试环境验证再上线。
- 容器 `TZ=Asia/Shanghai`，但数据库里的时间是 UTC（跨时区/换机器不会乱）。

## 10. 你可能遗漏、我补充的

1. **账号生命周期**：入职开号、调岗改权限、离职销号（Open WebUI 用户 + 网关数据）。
2. **用量看板**：`/admin/usage` 能看到每人每天 token 与请求数——这是内网「谁在用、要不要加卡」的唯一依据。
3. **内容审计**：`audit_log` + 会话导出，满足「出了事能查」。
4. **回滚预案**：镜像打 tag、数据库每日备份、权重目录只读挂载。
5. **容量规划**：先按「同时在线 ≤ 8 人、同时提问 ≤ 4 人」上线，观察一周再决定加卡。
6. **使用说明文档**：告诉用户上下文 8k、会幻觉、别传大文件——比事后解释省事。
7. **停机窗口**：模型加载 2~5 分钟，升级要选在没人用的时候，并提前通知。
8. **数据保留期**：会话/记忆保留多少天要写进制度（默认不自动删除）。
9. **域名与 HTTPS**：`http://IP:8080` 只适合先跑通，正式使用建议内网域名 + 证书。
10. **模型可见性**：Open WebUI 里按部门建组，只暴露这一个模型，避免用户误选 Ollama/其它连接。
