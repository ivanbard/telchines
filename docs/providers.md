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

## Stability

The `1.x` line treats the documented provider kinds and policy fields as stable. New provider kinds may be added in a backward-compatible way, but existing ones should not change wire shape without a documented migration.
