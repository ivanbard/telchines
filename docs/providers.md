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

- `base_url`
- `model`
- `api_key_env`
- `timeout_seconds`

### `local_command`

Local process that reads JSON on stdin and writes JSON on stdout.

Required fields:

- `command`
- `args`
- `timeout_seconds`

Optional fields:

- `env`

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

## Policy Rules

- `model_mode=local` blocks remote providers
- `model_mode=remote` blocks local command providers
- `model_mode=hybrid` allows both local and remote providers
- `no_egress=true` blocks networked providers even in hybrid mode

Use `tel providers list` to inspect which providers are allowed and why any provider is blocked.

Use `tel providers check [NAME]` to validate one provider, or omit `NAME` to check all providers. By default this performs a live transport check for `openai_compatible` and `local_command` providers. Add `--offline` to validate only configuration and policy:

```bash
tel providers check heuristic
tel providers check remote-generation
tel providers check --offline
```

`providers check` exits nonzero when a selected provider is blocked by policy, missing credentials, unreachable, or returns malformed data. It prints JSON first so the failure can be inspected in CI logs.

Telchines includes an optional live pytest smoke for OpenAI-compatible endpoints. It is skipped unless all of these environment variables are set:

```bash
export TELCHINES_INTEGRATION_OPENAI_BASE_URL=http://127.0.0.1:11434/v1
export TELCHINES_INTEGRATION_OPENAI_MODEL=llama3.1
export TELCHINES_INTEGRATION_OPENAI_API_KEY=dummy
pytest tests/test_provider_integration.py
```

For local servers that do not require authentication, use a harmless dummy token if the server ignores `Authorization`.

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
          "api_key_env": "TELCHINES_LOCAL_API_KEY",
          "timeout_seconds": 120
        }
      }
    }
  }
}
```

Some local servers do not require authentication but still expect a bearer value. Set a harmless local-only value:

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
  "env": {
    "TELCHINES_PROVIDER_MODE": "local"
  }
}
```

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
