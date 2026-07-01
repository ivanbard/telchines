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

You can also certify local OpenAI-compatible servers with the matrix harness:

```bash
TELCHINES_LIVE_LOCAL_OPENAI=1 \
TELCHINES_LOCAL_OPENAI_API_KEY=local \
python scripts/provider_capability_study.py \
  --matrix docs/provider-matrices/ollama.json \
  --include-live
```

LM Studio, llama.cpp server, vLLM, and LiteLLM presets live under `docs/provider-matrices/`.

## Local Command Wrapper

Use `local_command` when a local model needs a custom wrapper. The command is executed from the Telchines project root, receives the workflow request as JSON on stdin, and must emit a JSON object on stdout.

```json
{
  "kind": "local_command",
  "capabilities": ["repair", "generation"],
  "command": "python",
  "args": ["examples/providers/local_command_provider.py"],
  "timeout_seconds": 60,
  "output_limit_chars": 65536,
  "env": {
    "TELCHINES_PROVIDER_MODE": "local"
  }
}
```

Telchines parses the full stdout stream for the JSON object, then stores bounded stdout/stderr diagnostics using `output_limit_chars` so noisy local model runners do not bloat replay artifacts. The value must be at least 1024 characters.

Diagnostics send `workflow_type=provider_check`; wrappers should return any JSON object for that request. Workflow requests use:

- `workflow_type=compile_repair` with `files`, `observations`, and `retrieval_context`
- `workflow_type=spec_to_sva` with `spec`, `rtl`, `output_file`, and `retrieval_context`
- `workflow_type=dut_to_cocotb` with `dut`, optional `spec`, `output_dir`, `intent`, and `retrieval_context`
- generation requests also include `conventions`, mirroring `.tel/config.json` `generation` settings for output naming and clock/reset inference

When a `local_command` repair provider is used behind the optional `agent_runtime` pilot, retry requests may also include `previous_attempts` with validation summaries and normalized observations from earlier failed candidates. Wrappers can use that field to revise the next patch proposal.

Keep wrappers deterministic enough that generated artifacts can be reviewed and replayed. Do not store secrets directly in `.tel/config.json`; prefer env var names and a local secret manager or shell environment.
