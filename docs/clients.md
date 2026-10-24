# 把本机模型接进 Claude Code / DeepSeek Harness 等客户端

模型有两个入口，协议不同，选错就接不上：

| 入口 | 协议 | 谁来用 | 有无历史/记忆 |
| --- | --- | --- | --- |
| `http://<host>:9000/v1` | OpenAI Chat Completions | Open WebUI、任意 OpenAI SDK、DeepSeek Harness | ✅ 有（网关负责） |
| `http://<host>:8000/v1/messages` | **Anthropic Messages** | Claude Code（CLI / VS Code 扩展）、任何 Anthropic SDK | ❌ 无（直连 vLLM，绕过网关） |

Anthropic 那个入口是 vLLM 0.31 自带的（`vllm/entrypoints/anthropic/api_router.py` 里 `/v1/messages`、
`/v1/messages/count_tokens`），**不需要任何转换代理**，也不校验 `x-api-key`（占位即可）。

> 因为 vLLM 不校验 key，`8000` 端口只应在本机/内网可达，别暴露到公网。

---

## 1. Claude Code（终端）

### 1.1 先把端口打通

vLLM 默认只监听 `127.0.0.1:8000`。Claude Code 跑在你自己的开发机上时，用 SSH 隧道：

```bash
ssh -N -L 8000:127.0.0.1:8000 <user>@<部署机>
# 之后本机的 http://127.0.0.1:8000 就是那台机器上的 vLLM
```

（容器部署时同理：宿主机能连到容器 IP 就行，`ssh -L 8000:172.17.0.5:8000`。）

### 1.2 环境变量

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8000"      # 注意：不要带 /v1
export ANTHROPIC_AUTH_TOKEN="local-qwen3827b"          # vLLM 不校验，占位
export ANTHROPIC_MODEL="qwen3.8-27b"
export ANTHROPIC_SMALL_FAST_MODEL="qwen3.8-27b"        # 后台小任务用同一个模型
export ANTHROPIC_DEFAULT_HAIKU_MODEL="qwen3.8-27b"
export ANTHROPIC_DEFAULT_SONNET_MODEL="qwen3.8-27b"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1      # 别连 Anthropic 的遥测/更新

claude          # 或 claude -p "解释一下这个仓库"
```

本仓库带了脚本，省得手打：

```bash
bash scripts/claude_code_env.sh              # 只打印 export，自己 eval
eval "$(bash scripts/claude_code_env.sh)"
bash scripts/claude_code_env.sh --run        # 设好环境直接起 claude
bash scripts/claude_code_env.sh --code       # 设好环境后起 VS Code（扩展继承）
```

### 1.3 VS Code 扩展

扩展本质是调用同一个 CLI，所以两条路都行：

- **推荐**：从已经导出环境变量的终端里 `code /path/to/project`，扩展继承该环境。
- 或者写进用户设置 `~/.claude/settings.json`：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8000",
    "ANTHROPIC_AUTH_TOKEN": "local-qwen3827b",
    "ANTHROPIC_MODEL": "qwen3.8-27b",
    "ANTHROPIC_SMALL_FAST_MODEL": "qwen3.8-27b",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen3.8-27b",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "qwen3.8-27b",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  }
}
```

### 1.4 硬限制：上下文

Claude Code 的系统提示 + 工具定义本身就占 **约 20k tokens**，还没算你的文件内容。所以：

- `MAX_MODEL_LEN` 必须 ≥ 32768（本工程已改为此值），8k 完全不够，会在第一轮就 400/截断；
- 32k 只够「小任务、单文件、短会话」，长会话会撞上限，表现为 `stop_reason: max_tokens` 或上游 400；
- 实测：30k tokens 的请求（系统提示 + 工具）预填充约 **23s**（TP=4 走 PCIe，prefill 是瓶颈），
  同一会话后续轮次有前缀缓存会快一些。

**结论：能用，但别指望它像云端 Claude 那样横扫整个仓库。** 适合：单文件改写、解释某段代码、小脚本生成。

### 1.5 并发

`MAX_NUM_SEQS` 现在是 2（32k 上下文下 KV 容量约 64k tokens，等于 2 路 32k）。一个人用 Claude Code
时别再让同事跑长上下文任务，否则 vLLM 会抢占（日志出现 `preempt`）。

---

## 2. DeepSeek Harness

DSH 通过 `llm-pi-ai` 适配器接入任意 OpenAI / Anthropic 兼容端点，**推荐在 Web GUI 里加**（会写进
profile 的 settings 层，重启后仍在）：

1. 打开 DSH Web 界面 → **设置 → 模型（Models）** → 添加 Provider；
2. 填：
   | 字段 | 值 |
   | --- | --- |
   | 名称 / route | `llm-serving`（随便，只要唯一） |
   | 协议 protocol | `openai-completions` |
   | Base URL | `http://<部署机IP>:9000/v1`（走网关，有历史/记忆/配额） |
   | API Key | 网关开了 `auth.api_keys` 时填 `GATEWAY_API_KEY`；当前 `.env` 没开就随便填 |
   | 模型 id | `qwen3.8-27b` |
   | Context window | `32768` |
   | Max output tokens | `2048` ~ `4096` |

3. 保存后在模型选择器里选 `llm-serving / qwen3.8-27b` 即可。

如果不想用 GUI，也可以在 profile 的 patch 层（`$DSH_HOME/profiles/<name>/cordis.patch.yml`）里加：

```yaml
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      llm-serving:
        displayName: 内网 Qwen3.8-27B
        api: openai-completions
        baseURL: http://127.0.0.1:9000/v1
        models:
          - id: qwen3.8-27b
            contextWindow: 32768
            maxTokens: 4096
```

密钥走 `apiKeyEnv`（引用环境变量名）与 `$DSH_HOME/.credentials.yaml`，不要把明文 key 写进配置。

> DSH 是 agent 型工具（大量工具调用 + 长上下文 + 子代理），32k 上下文会比 Claude Code 更吃紧。
> 建议先用它做「问答/单文件任务」，跑通了再考虑加卡或换更大上下文模型。

---

## 3. 想两者兼得？（可选增强）

走网关只有 OpenAI 协议，Claude Code 用不了；直连 vLLM 又丢了历史/记忆/配额/审计。要兼得，需要给网关加一个
`POST /v1/messages` 的 Anthropic→OpenAI 转换路由（转发 + 解析 Anthropic SSE 记账）。当前没做，
因为 Claude Code 的主要诉求是「能用起来」，而 Open WebUI 那条链路已经完整覆盖内网同事的日常使用。

---

## 4. 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| Claude Code 报 `API Error: 404 ... /v1/messages` | `ANTHROPIC_BASE_URL` 填成了 `.../v1` 或指向了只有 OpenAI 协议的网关 | 填 `http://host:8000`（不带 `/v1`），直连 vLLM |
| `400 maximum context length` | 请求超过 `MAX_MODEL_LEN` | 加到 32768；或缩短会话/关掉大文件读取 |
| 第一轮就超时 | 20k+ 系统提示的 prefill 要十几秒，客户端默认超时短 | 调大客户端超时；先发一句短的预热 |
| 回答里有 `</think>` | 没开 reasoning parser | `.env` 的 `VLLM_EXTRA_ARGS` 加 `--reasoning-parser qwen3` |
| DSH 里模型列表是空的 | provider 没保存成功 / baseURL 多了 `/v1` | 先去 GUI 确认；用 `curl <baseURL>/models` 验证 |
| DSH 工具调用报 `tool_choice` 相关错误 | vLLM 没开工具解析 | 加 `--enable-auto-tool-choice --tool-call-parser qwen3_coder` |
