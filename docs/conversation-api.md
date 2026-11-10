<!--
SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Conversation API

Clients ask questions through the authenticated Next.js gateway:

```text
POST /api/chat/completions
```

Authenticate with a session cookie or an API token (see
[authentication.md](./authentication.md)). The request and response schemas are
in [`openapi/auto-ontology-api.json`](./openapi/auto-ontology-api.json).

## Response stream

The response is a server-sent event stream. Each event's `data:` line holds one
JSON object, and the stream ends with `data: [DONE]`, a final line that isn't
JSON.

| `type`   | Carries |
| -------- | ------- |
| `step`   | An agent step starting or ending (`node`, `phase`, and `thought` on end), for showing progress. |
| `sql`    | A query that passed validation and is about to run. |
| `result` | The final `answer`: `response` (prose), `sql_code`, `sql_response_from_db` (rows), `thoughts` (step trace). |
| `charts` | A chart or table for the result (`content`, `sql_response`). Sent after `result` when the query ran and the request includes a `conversation_id`. |
| `error`  | A `message` describing why the run failed. |
| `clarification` | The run stopped to ask what a business term means, instead of answering. See [Clarification questions](#clarification-questions). |

A run makes many sequential model calls and typically takes tens of seconds, so
read the stream as it arrives:

```python
import json
import os

import requests

response = requests.post(
    "https://auto-ontology.example.com/api/chat/completions",
    headers={"x-api-key": os.environ["AUTO_ONTOLOGY_API_TOKEN"]},
    json={"question": "How many orders shipped last week?"},
    stream=True,
    timeout=900,
)
response.raise_for_status()

for line in response.iter_lines(decode_unicode=True):
    if not line or not line.startswith("data:"):
        continue
    payload = line.removeprefix("data:").strip()
    if payload == "[DONE]":
        break
    event = json.loads(payload)
    if event["type"] == "step":
        print("…", event.get("node"), event.get("phase"))
    elif event["type"] == "result":
        answer = event["answer"]
        print(answer["response"])
        print(answer["sql_code"])
    elif event["type"] == "error":
        raise RuntimeError(event["message"])
```

The MCP server's `ask_question` tool wraps this same call and returns the
answer, SQL, and rows as separate fields; see [mcp.md](./mcp.md).

## Conversations

`conversation_id` is optional:

- Omit it for a stateless, one-shot request.
- Send a client-generated UUID to start or continue a conversation, and reuse it
  for every turn in the thread.
- Wait for `[DONE]` before sending the next turn. An overlapping request on the
  same conversation returns `409 Conversation in progress`.

```json
{
  "question": "What about last month?",
  "conversation_id": "d61d8aa3-d496-4fa7-97ce-4f831c162e7f"
}
```

For a conversational turn, the backend loads up to the five most recent
completed turns (capped at 12,000 characters) and rewrites a contextual
follow-up such as "What about August?" into a standalone question before
running the text-to-SQL agent. Independent questions pass through unchanged.
Only prior questions, answers, and SQL feed that rewrite; agent thoughts, chart
payloads, errors, and raw result rows stay out of it.

Conversation history belongs to the authenticated user. An API-created
conversation opens in the UI at `/chat?focus=<conversation_id>` for the user who
owns it. The web UI manages its own conversation IDs.

## Clarification questions

"How many VIP customers do we have?" has no answer until "VIP" is defined. A
model that is not told will invent a definition and answer with confidence.
When this feature is on, the chat can instead end a turn with a short question,
like Claude's own question tool: a few concrete readings to pick from, plus a
free-text answer.

It is **off by default**. It is switched on by the `clarification_enabled` row of
the configuration table (`true`), read for every request, and a client must
also ask for it with `supports_clarification: true`. A client that does not
(the MCP server, scripts, the evaluation harness) never receives one, so an
existing integration is never left waiting on a question it cannot show.
A stateless request (no `conversation_id`) never receives one either: with no
conversation there is nowhere to hold the question or receive the answer.

### The event

The stream ends with `[DONE]` right after it, exactly as after an answer; no
connection is held open while the user thinks.

```json
{
  "type": "clarification",
  "clarification_id": "5b0f9f0e-5f0a-4c53-9f57-3c9a1d0d6a11",
  "term": "VIP customers",
  "question": "What do you mean by VIP customers?",
  "options": [
    {"id": "opt-1", "label": "Total payments above the average"},
    {"id": "opt-2", "label": "Top 10% by total payments", "description": "Ranked by what each customer paid"},
    {"id": "opt-3", "label": "Above a fixed amount"}
  ],
  "allow_other": true
}
```

`options` holds 2 to 4 distinct readings; `description` is optional. None is a
vague "other": `allow_other` tells the client to add its own free-text entry.
`clarification_id` is generated by the server for each question.

### Answering

The answer is the next request on the same `conversation_id`. Send the option
(or the typed text) in `clarification`, and put the same words in `question` so
the conversation shows what was answered:

```json
{
  "question": "Top 10% by total payments",
  "conversation_id": "d61d8aa3-d496-4fa7-97ce-4f831c162e7f",
  "supports_clarification": true,
  "clarification": {"clarification_id": "5b0f9f0e-...", "option_id": "opt-2"}
}
```

For "Other", send `"clarification": {"clarification_id": "...", "text": "above 100 EUR"}`.
Exactly one of `option_id` and `text` is required.

A plain message with no `clarification` field also works as the answer, for
clients that cannot send one: a short model call decides whether the message
answers the question or is a new, unrelated question, and treats it as the
answer when unsure.

The server then runs the original question again with the answer as
authoritative evidence (`The user defined "VIP customers" as: ...`), and the
earlier conversation as context. A question is never asked twice for the same
original question.

The request is refused with `422` when `clarification_id` is not the question
pending in that conversation: an old one, one already answered, one from
another conversation, an `option_id` that was not offered, or an answer with no
`conversation_id`. A conversation owned by another user is `404`, as always.
A question that was already shown still resumes if the feature is switched off
meanwhile.

### What is stored

The question is saved as an assistant message so a reload shows it. There is no
column for structured parts, so the `content` is the question followed by a
fenced block, as charts are:

````text
What do you mean by VIP customers?

```clarification
{"clarification_id": "...", "term": "VIP customers", "question": "...", "options": [...], "allow_other": true, "original_question": "How many VIP customers do we have?"}
```
````

A clarification is *pending* exactly when it is the last message of its
conversation. The answer is the next `user` message. The model's context holds
the question, the options offered, and the answer.

### When it asks

Rarely, and never on a guess. It does not ask when:

- the caller supplied `evidence` for the question;
- a verified definition (a business definition, saved analysis or glossary term)
  matches the question closely;
- the term is a table, column or value of the schema, or the question gives its
  own definition or threshold;
- the possible readings would not change the SQL materially;
- no conversation, no semantic index, or no model is available, or anything in
  the check fails: the chat then answers as it always did.

At most one question is asked per original question. Every decision is logged
with its reason (`Clarification decision: <reason>`), for example
`skipped_verified_definition`, `llm_schema_reference`, `invalid_proposal` or
`asked`, with the evidence it was based on.

## Trust boundary

Call the Next.js gateway (`APP_URL`, port 3000 in Docker Compose). It resolves
the user and forwards an internal `x-auto-ontology-user-id` header to FastAPI
(port 3001). FastAPI trusts that header without verifying it, so keep port 3001
on a private network. A direct FastAPI request without `conversation_id` stays
stateless; one with `conversation_id` requires the trusted identity header.
