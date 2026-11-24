# Integrations

## flow2api-mcp + flow2api-new-skill

让 Claude Code / Codex / Antigravity 等 Agent 通过本机 Flow2API 生图。独立仓库：https://github.com/0sunsongyang0/flow2api-skill-mcp

- `flow2api-mcp/`：Python MCP 服务，提供单图生成、多账号批量生成、模型/账号列表、连接检查等工具，也可作命令行使用。详见 [flow2api-mcp/README.md](flow2api-mcp/README.md)。
- `flow2api-new-skill/SKILL.md`：配套的 Agent skill。

### 安装

```bash
cd integrations/flow2api-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
```

在 `integrations/flow2api-mcp/` 下创建私密配置 `settings.json`（已被 .gitignore 忽略，切勿提交）：

```json
{
  "base_url": "http://127.0.0.1:38000/v1",
  "api_key": "<你的 Flow2API API Key>",
  "output_directory": "/absolute/path/for/images",
  "job_timeout_seconds": 600
}
```

注册 MCP（以 Claude Code 为例）：

```bash
claude mcp add flow2api -- /path/to/flow2api-mcp/.venv/bin/python /path/to/flow2api-mcp/server.py
```

把 `flow2api-new-skill` 复制到 `~/.claude/skills/`（或对应客户端的 skills 目录），并把 SKILL.md 中的 `/path/to/flow2api-mcp` 替换为实际路径。

### 测试

```bash
cd integrations/flow2api-mcp && .venv/bin/python -m unittest
```
