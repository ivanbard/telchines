# Provider Configuration

Telchines routes model-backed work by capability. In `v1`, the relevant capabilities are:

- `repair`
- `generation`

## Supported Provider Kinds

### `heuristic`

Built-in deterministic provider used by default for benchmarkable local behavior.

### `openai_compatible`

Remote HTTP provider for hosted generation or repair.

Required fields:

- `base_url`, an `http://` or `https://` URL with any path prefix such as `/v1`
- `model`
- `api_key_env`
- `timeout_seconds`

Optional fields:

- `endpoint`, a relative path such as `chat/completions`; leading slashes are tolerated, and base URL path prefixes like `/v1` are preserved
- `headers` for extra string-valued HTTP headers; `Authorization` is reserved and always derived from `api_key_env`
- `auth`, either `bearer` (default) or `none`; use `none` for local servers that do not require an `Authorization` header

### `anthropic`

Native Anthropic Messages API provider for hosted Claude models.

Required fields:

- `model`
- `api_key_env`
- `timeout_seconds`

Optional fields:

- `base_url`, defaulting to `https://api.anthropic.com/v1`
- `endpoint`, defaulting to `messages`
- `anthropic_version`, defaulting to `2023-06-01`
- `max_tokens`, defaulting to `4096`
- `headers` for extra string-valued HTTP headers; `x-api-key`, `anthropic-version`, and `content-type` are reserved

### `local_command`

Local process that reads JSON on stdin and writes JSON on stdout.

Required fields:

- `command`
- `args`
- `timeout_seconds`

Optional fields:

- `env`
- `output_limit_chars` for persisted stdout/stderr diagnostics; it must be an integer of at least 1024 and defaults to 65536

### `agent_runtime`

Optional compile-repair pilot that runs a bounded agent loop over an existing repair provider. The runtime proposes a patch through `base_provider`, validates it with the normal Telchines repair validation path, and retries with validation feedback until a patch passes or `max_iterations` is exhausted.

Required fields:

- `runtime`, currently `langgraph`
- `base_provider`, referencing an `openai_compatible`, `anthropic`, or `local_command` repair provider
- `capabilities`, currently `["repair"]`
- `timeout_seconds`

Optional fields:

- `max_iterations`, a positive integer that defaults to 3

Install the optional LangChain/LangGraph dependencies with:

```bash
pip install "telchines[agentic]"
```

The runtime is intentionally opt-in and does not replace Telchines retrieval, run storage, policy checks, or validation gates. It inherits policy blocking from its base provider, so `no_egress=true`, `model_mode=local`, and `model_mode=remote` still apply through the delegated provider.

## Example Config

```json
{
  "model_mode": "hybrid",
  "no_egress": false,
  "project": {
    "model_policy": {
      "default_provider_by_capability": {
        "repair": "heuristic",
        "generation": "remote-generation"
      },
      "providers": {
        "heuristic": {
          "kind": "heuristic",
          "capabilities": ["repair", "generation"]
        },
        "remote-generation": {
          "kind": "openai_compatible",
          "capabilities": ["generation"],
          "base_url": "https://example-provider.local/v1",
          "model": "verification-model",
          "api_key_env": "TELCHINES_API_KEY",
          "timeout_seconds": 30
        }
      }
    }
  }
}
```

Agent-runtime repair pilot:

```json
{
  "kind": "agent_runtime",
  "runtime": "langgraph",
  "base_provider": "local-repair-model",
  "capabilities": ["repair"],
  "max_iterations": 3,
  "timeout_seconds": 60
}
```

## Policy Rules

- `model_mode=local` blocks external HTTP providers, while allowing built-in, local command, and loopback local HTTP providers
- `model_mode=remote` blocks local command providers
- `model_mode=hybrid` allows both local and remote providers
- `no_egress=true` blocks external HTTP providers even in hybrid mode

Use `tel providers list` to inspect which providers are allowed and why any provider is blocked.

Use `tel providers check [NAME]` to validate one provider, or omit `NAME` to check all providers. By default this performs a live transport check for `openai_compatible` and `local_command` providers and reports `agent_runtime` routing metadata. Add `--offline` to validate only configuration and policy:

```bash
tel providers check heuristic
tel providers check remote-generation
tel providers check --offline
```

`providers check` exits nonzero when a selected provider is blocked by policy, missing credentials, unreachable, or returns malformed data. It prints JSON first so the failure can be inspected in CI logs.

For broader certification across hosted, local OpenAI-compatible, local command, and agent-runtime providers, use the matrix-backed harness:

```bash
python scripts/provider_capability_study.py --matrix docs/provider-matrices/openrouter.json --dry-run
python scripts/provider_capability_study.py --matrix docs/provider-matrices/local_command.json
```

Live hosted or local HTTP matrices require both `--include-live` and the matrix's `TELCHINES_LIVE_*` gate. See `docs/provider-capability-study.md`.

Telchines includes an optional live pytest smoke for OpenAI-compatible endpoints. It is skipped unless all of these environment variables are set:

```bash
export TELCHINES_INTEGRATION_OPENAI_BASE_URL=http://127.0.0.1:11434/v1
export TELCHINES_INTEGRATION_OPENAI_MODEL=llama3.1
export TELCHINES_INTEGRATION_OPENAI_API_KEY=dummy
pytest tests/test_provider_integration.py
```

For local servers that do not require authentication, use a harmless dummy token if the server ignores `Authorization`.

OpenAI-compatible responses may return the Telchines JSON object either in `choices[].message.content` or in function/tool-call `arguments`. This keeps hosted APIs and local gateways with structured-output/tool-call modes on the same contract.

## Real OpenAI-Compatible Examples

Hosted OpenAI-compatible endpoint:

```json
{
  "model_mode": "hybrid",
  "no_egress": false,
  "project": {
    "model_policy": {
      "default_provider_by_capability": {
        "repair": "openai-repair",
        "generation": "openai-generation"
      },
      "providers": {
        "heuristic": {
          "kind": "heuristic",
          "capabilities": ["repair", "generation"]
        },
        "openai-repair": {
          "kind": "openai_compatible",
          "capabilities": ["repair"],
          "base_url": "https://api.openai.com/v1",
          "model": "gpt-4.1-mini",
          "api_key_env": "OPENAI_API_KEY",
          "timeout_seconds": 30
        },
        "openai-generation": {
          "kind": "openai_compatible",
          "capabilities": ["generation"],
          "base_url": "https://api.openai.com/v1",
          "model": "gpt-4.1",
          "api_key_env": "OPENAI_API_KEY",
          "timeout_seconds": 60
        }
      }
    }
  }
}
```

Native Anthropic endpoint:

```json
{
  "model_mode": "hybrid",
  "no_egress": false,
  "project": {
    "model_policy": {
      "default_provider_by_capability": {
        "repair": "anthropic-dev",
        "generation": "anthropic-dev"
      },
      "providers": {
        "anthropic-dev": {
          "kind": "anthropic",
          "capabilities": ["repair", "generation"],
          "model": "claude-3-5-sonnet-latest",
          "api_key_env": "ANTHROPIC_API_KEY",
          "max_tokens": 4096,
          "timeout_seconds": 90
        }
      }
    }
  }
}
```

OpenRouter is also OpenAI-compatible. Set `OPENROUTER_API_KEY` in your shell or project-local ignored `.env`, choose a model ID from OpenRouter's model list, and point the provider at `/api/v1`:

```json
{
  "model_mode": "hybrid",
  "no_egress": false,
  "project": {
    "model_policy": {
      "default_provider_by_capability": {
        "repair": "openrouter-dev",
        "generation": "openrouter-dev"
      },
      "providers": {
        "heuristic": {
          "kind": "heuristic",
          "capabilities": ["repair", "generation"]
        },
        "openrouter-dev": {
          "kind": "openai_compatible",
          "capabilities": ["repair", "generation"],
          "base_url": "https://openrouter.ai/api/v1",
          "model": "cohere/north-mini-code:free",
          "api_key_env": "OPENROUTER_API_KEY",
          "timeout_seconds": 30
        }
      }
    }
  }
}
```

Local OpenAI-compatible server such as Ollama, LM Studio, llama.cpp server, vLLM, or LiteLLM:

```json
{
  "model_mode": "hybrid",
  "no_egress": false,
  "project": {
    "model_policy": {
      "default_provider_by_capability": {
        "generation": "local-openai"
      },
      "providers": {
        "heuristic": {
          "kind": "heuristic",
          "capabilities": ["repair", "generation"]
        },
        "local-openai": {
          "kind": "openai_compatible",
          "capabilities": ["generation"],
          "base_url": "http://127.0.0.1:11434/v1",
          "model": "qwen2.5-coder",
          "auth": "none",
          "timeout_seconds": 120
        }
      }
    }
  }
}
```

Some local servers do not require authentication. Set `auth` to `none` to avoid sending an `Authorization` header. If a server still expects a bearer value, keep the default auth mode and set a harmless local-only value:

```bash
export TELCHINES_LOCAL_API_KEY=local
tel providers check local-openai
```

## Local Command Providers

`local_command` providers execute the configured command from the project root. Treat them like scripts in your build system: review them before enabling, avoid storing secrets directly in `.tel/config.json`, and prefer environment variable names over literal secret values.

The command receives a JSON request on stdin and must write a JSON object on stdout. Logs before or after the JSON object are tolerated as long as a valid JSON object appears in stdout.

Minimal config:

```json
{
  "kind": "local_command",
  "capabilities": ["repair", "generation"],
  "command": "python",
  "args": ["examples/providers/local_command_provider.py"],
  "timeout_seconds": 30,
  "output_limit_chars": 65536,
  "env": {
    "TELCHINES_PROVIDER_MODE": "local"
  }
}
```

Telchines parses the full stdout stream for the JSON object, then bounds persisted stdout/stderr in task artifacts. Use `output_limit_chars` when wrappers or local model runners emit verbose logs.

Expected repair response:

```json
{
  "status": "proposed",
  "file_path": "rtl/example.sv",
  "candidate_content": "module example; endmodule\n",
  "explanation": "Explain the proposed change.",
  "evidence_paths": ["rtl/example.sv"]
}
```

Expected SVA generation response:

```json
{
  "status": "proposed",
  "file_path": ".tel/artifacts/generated/example_assertions.sv",
  "candidate_content": "module example_assertions; endmodule\n",
  "explanation": "Explain the assertion draft.",
  "evidence_paths": ["docs/spec.md", "rtl/example.sv"],
  "properties": [
    {
      "name": "p_example",
      "summary": "Short property summary.",
      "rationale": "Why this property is grounded.",
      "source_citation": "docs/spec.md"
    }
  ]
}
```

Expected cocotb generation response:

```json
{
  "status": "proposed",
  "file_path": ".tel/artifacts/generated/cocotb/test_example.py",
  "manifest_path": ".tel/artifacts/generated/cocotb/example_cocotb_manifest.json",
  "candidate_content": "import cocotb\n",
  "top_module": "example",
  "explanation": "Explain the scaffold.",
  "assumptions": ["Clock/reset inferred from DUT ports."],
  "ports": [
    {"name": "clk", "direction": "input", "width": 1, "role": "clock"}
  ],
  "evidence_paths": ["rtl/example.sv"]
}
```

Provider-returned file paths must stay inside the project. Telchines rejects absolute paths outside the project and relative paths that escape with `..`.

## Artifact Redaction

Task artifacts are persisted under `.tel/task-artifacts`. Telchines redacts dictionary fields whose keys look like credentials, tokens, passwords, API keys, or authorization headers before saving task artifacts. RTL, specs, prompts, and model responses are still stored for replayability, so use `no_egress=true`, local providers, and artifact cleanup practices appropriate for proprietary projects.

## Stability

The `1.x` line treats the documented provider kinds and policy fields as stable. New provider kinds may be added in a backward-compatible way, but existing ones should not change wire shape without a documented migration.
