---
inclusion: fileMatch
fileMatchPattern: '**/*'
---

# Bedrock and Strands

FCB reaches every LLM capability — vision, classification, message
generation — through Amazon Bedrock. There is no fallback to other
providers. Strands is the agent framework that wraps Bedrock calls with
tool-use and message plumbing.

## Model

The pinned model id is:

```
global.anthropic.claude-opus-4-6-v1
```

The value MUST be read from the `BEDROCK_MODEL_ID` env var so it can be
bumped without a code change, but the default and the intended production
value is exactly the string above.

The `global.` prefix denotes a cross-region inference profile; the client
region is `us-west-2` (see `deployment.md`). No other Bedrock model ids
are used in this project.

## Access

Credentials come exclusively from the instance's IAM role
(`BedrockforEC2`). The default boto3 credential chain resolves them
transparently. FCB code MUST NOT accept AWS access keys via `.env` or
config, and MUST NOT construct a session with hardcoded credentials.

## Wiring

One Bedrock model instance is constructed at import time in
`src/fcb/agents/__init__.py` and shared by every agent module in that
package. Constructing a fresh client per invocation is prohibited because
it defeats connection pooling and increases cold-start latency.

The shared instance:

```python
# src/fcb/agents/__init__.py
from strands.models import BedrockModel
from fcb import config

bedrock_model = BedrockModel(
    model_id=config.BEDROCK_MODEL_ID,
    region_name=config.AWS_REGION,
)
```

And each agent module imports it:

```python
# src/fcb/agents/vision.py
from strands import Agent
from fcb.agents import bedrock_model

vision_agent = Agent(model=bedrock_model, system_prompt=...)
```

`fcb.agents.bedrock_model` is not a separate dependency; it is the single
package-scoped shared model instance. Strands itself is the dependency.

The three agents documented in `architecture.md` share one model
instance:

| Agent          | Uses vision? | System prompt source                   |
|----------------|--------------|----------------------------------------|
| `vision`       | Yes          | Compiled from current `events.prompt`  |
| `classify`     | No           | Static, in-repo                        |
| `voice`        | No           | Static, in-repo                        |

The `vision` agent's system prompt is not static: it is rebuilt each call
from the active event's editable prompt so admins can retune scoring
without a code change.

## Cost and Latency

Vision calls are the expensive path. FCB MUST invoke the vision agent
only when `classify` has decided a message is a workout update, or when
an admin explicitly re-runs it from the dashboard. Blind invocation on
every channel message is prohibited.

## Failure Handling

Bedrock invocation errors follow the fail-fast rule from
`project-guidelines.md`. Retries are appropriate only for throttling
(`ThrottlingException`) and transient network faults. Model errors,
credential errors, and content-policy rejections MUST surface as full
stack traces via the logger and MUST NOT be swallowed to a generic
"couldn't process" reply.

The user-facing response when an agent call fails (e.g., the bot posting
back "I couldn't read that screenshot, please try again") is generated
from the caller after the exception is logged with its trace, not from
inside an `except` that hides the cause.
