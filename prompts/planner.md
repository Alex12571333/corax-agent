# Planner prompt template

Given a **goal** and the list of **available capabilities**, produce a plan
as a JSON object:

```json
{
  "goal": "<the user goal>",
  "tasks": [
    { "id": "task-1", "capability": "<capability id>", "input": { } }
  ]
}
```

## Rules

- Use only capability ids from the provided catalogue.
- Each task's `input` must match that capability's documented schema.
- Keep the plan minimal and ordered; later tasks may depend on earlier ones.
- Do not exceed `limits.max_plan_tasks` tasks.
- Do not assume a tool exists because it is mentioned in this prompt. The
  runtime-provided capability catalogue is the source of truth.
