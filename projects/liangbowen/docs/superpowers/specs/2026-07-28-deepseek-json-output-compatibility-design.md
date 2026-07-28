# DeepSeek JSON Output Compatibility Design

## Goal

Enable the existing AI grading pipeline to use the user's DeepSeek API key through DeepSeek's OpenAI-compatible Chat Completions API without weakening validation for other providers or persisting the secret in Git.

## Current state and root cause

The application already supports `mock` and `openai-compatible` providers. The generic provider currently always sends OpenAI's strict `response_format.type=json_schema` request. DeepSeek authenticates successfully and lists `deepseek-v4-flash`, but its API rejects that request shape with HTTP 400. DeepSeek documents `response_format.type=json_object` for JSON Output.

The local shell exports `DEEPSEEK_API_KEY`. The ignored project `.env` can reference it as `${DEEPSEEK_API_KEY}`, so no key value needs to be copied into a tracked file.

## Selected approach

Add an explicit response-format setting instead of detecting DeepSeek from its URL.

- `AGENT_RESPONSE_FORMAT=json-schema` remains the default, preserving current behavior for existing OpenAI-compatible providers.
- `AGENT_RESPONSE_FORMAT=json-object` sends DeepSeek's supported `{"type":"json_object"}` request.
- The provider continues to request JSON in its trusted system instructions.
- Existing application-side schema parsing, validation, bounded repair, retry, and failure handling remain authoritative. DeepSeek JSON mode guarantees valid JSON, while the application validates the exact grading contract.

Explicit configuration avoids hidden hostname-based behavior and supports other providers with the same capability profile.

## Configuration

Add a typed application setting accepting only:

- `json-schema`
- `json-object`

Pass it through Docker Compose to the API, worker, and beat services and from the provider factory into `OpenAICompatibleProvider`.

The local ignored `.env` will use:

```dotenv
AGENT_PROVIDER=openai-compatible
AGENT_BASE_URL=https://api.deepseek.com
AGENT_MODEL=deepseek-v4-flash
AGENT_API_KEY=${DEEPSEEK_API_KEY}
AGENT_RESPONSE_FORMAT=json-object
```

The tracked `.env.example` and README will document the generic setting without containing a real credential.

## Request flow

1. API or worker loads the typed provider settings.
2. The factory creates `OpenAICompatibleProvider` with the selected response-format mode.
3. For `json-schema`, the provider sends the existing strict JSON Schema payload unchanged.
4. For `json-object`, the provider sends `{"type":"json_object"}`.
5. The returned content passes through the existing bounded JSON decoder and exact evaluation-output validation.
6. Invalid or incomplete model output follows the existing bounded repair path; upstream authentication, rate-limit, timeout, and protocol errors keep their current sanitized classifications.

## Security and operational behavior

- Never log or persist the API key.
- Keep HTTPS validation and existing redirect, proxy, timeout, and response-size protections.
- Do not add provider hostname auto-detection.
- Do not change grading database schemas or public API contracts.
- Recreate API, worker, and beat after changing `.env` so all grading paths use the same provider configuration.

## Testing and acceptance

Automated tests will prove:

- the default mode still emits the current strict `json_schema` body;
- `json-object` emits only DeepSeek's supported response format;
- invalid response-format configuration fails at startup;
- the factory forwards the configured mode;
- Docker Compose propagates the setting to API, worker, and beat;
- example configuration and documentation remain aligned.

After unit and integration tests pass, acceptance requires:

- Docker services are healthy with `AGENT_PROVIDER=openai-compatible`;
- runtime configuration reports the DeepSeek URL, model, response format, and a set-but-redacted key;
- a small real grading request completes and produces an application-valid evaluation report;
- the full project verification suite passes;
- no secret or ignored `.env` content is committed or pushed.

## Alternatives not selected

Hostname auto-detection is smaller but introduces surprising behavior and couples generic provider code to DeepSeek. Strict tool calling could carry a JSON Schema, but it is a larger protocol change, changes response extraction, and relies on a different API feature. The explicit response-format mode is the smallest stable extension of the existing architecture.
