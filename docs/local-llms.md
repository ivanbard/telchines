# Local LLMs

Telchines supports local models through two stable `v1` paths:

- `openai_compatible` for local HTTP servers such as Ollama, LM Studio, llama.cpp server, vLLM, or LiteLLM
- `local_command` for a wrapper script that reads JSON on stdin and writes JSON on stdout

## Local OpenAI-Compatible Server

Example Ollama-style config:

```json
{
  "model_mode": "hybrid",
  "no_egress": false,
  "project": {
    "model_policy": {
      "default_provider_by_capability": {
        "generation": "ollama-local"
      },
      "providers": {
        "heuristic": {
          "kind": "heuristic",
          "capabilities": ["repair", "generation"]
        },
        "ollama-local": {
          "kind": "openai_compatible",
          "capabilities": ["generation"],
          "base_url": "http://127.0.0.1:11434/v1",
          "model": "qwen2.5-coder",
          "api_key_env": "TELCHINES_LOCAL_API_KEY",
          "timeout_seconds": 120
        }
      }
    }
  }
}
```

Run a diagnostic before using it for generation:

```bash
export TELCHINES_LOCAL_API_KEY=local
tel providers check ollama-local
```

LM Studio and other local OpenAI-compatible servers use the same shape. Change `base_url`, `model`, and `timeout_seconds` to match the server.

## Local Command Wrapper

Use `local_command` when a local model needs a custom wrapper. The command is executed from the Telchines project root, receives the workflow request as JSON on stdin, and must emit a JSON object on stdout.

```json
{
  "kind": "local_command",
  "capabilities": ["repair", "generation"],
  "command": "python",
  "args": ["examples/providers/local_command_provider.py"],
  "timeout_seconds": 60,
  "env": {
    "TELCHINES_PROVIDER_MODE": "local"
  }
}
```

Diagnostics send `workflow_type=provider_check`; wrappers should return any JSON object for that request. Workflow requests use:

- `workflow_type=compile_repair` with `files`, `observations`, and `retrieval_context`
- `workflow_type=spec_to_sva` with `spec`, `rtl`, `output_file`, and `retrieval_context`
- `workflow_type=dut_to_cocotb` with `dut`, optional `spec`, `output_dir`, `intent`, and `retrieval_context`
- generation requests also include `conventions`, mirroring `.tel/config.json` `generation` settings for output naming and clock/reset inference

Keep wrappers deterministic enough that generated artifacts can be reviewed and replayed. Do not store secrets directly in `.tel/config.json`; prefer env var names and a local secret manager or shell environment.
