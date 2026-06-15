# Planner prompt

> Placeholder used by the future LLM planner. The built-in
> [`StubPlanner`](../corax/planner/stub.py) ignores it and emits a single
> `echo` task; a real planner will render this template with the goal and
> the live capability catalogue.

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
