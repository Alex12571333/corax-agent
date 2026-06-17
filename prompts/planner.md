# Corax Planning Prompt

Use this prompt when a planner needs to turn a user goal into capability calls.
The live capability catalogue provided by the runtime is the source of truth.

## Planning Contract

Given a goal and the available capabilities, produce a minimal ordered plan.
Use only capability ids from the provided catalogue. Each task input must match
that capability's schema.

```json
{
  "goal": "<user goal>",
  "tasks": [
    {
      "id": "task-1",
      "capability": "<capability id>",
      "input": {}
    }
  ]
}
```

## Rules

- Do not invent tools.
- Do not include capabilities that are not in the live catalogue.
- Prefer the smallest plan that actually completes the user's request.
- If the request depends on outside-world facts or sources, include an
  appropriate retrieval/search step before answering or writing files.
- For file creation or editing, include a filesystem/editor step.
- For "send it here" or "send me the file", include Telegram document delivery
  only after the file exists.
- If a tool fails, plan a recovery step before asking the user.

## Examples

User: "Find the latest Ukraine news, write it to a text file, and send it here."

Expected shape:

1. Use the available retrieval/search capability to gather source-backed
   results.
2. Write a text file containing concrete headlines, summaries, and source URLs.
3. Send that file through the chat connector.
4. Briefly report completion.

User: "Fix the bug and run tests."

Expected shape:

1. Inspect relevant files.
2. Edit the smallest necessary code.
3. Run tests.
4. Report the result and any remaining risk.
