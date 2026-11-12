# Chat agent module

This module contains the data-science-owned part of the chat: Pydantic AI
instructions, typed output, and read-only tool definitions. The model is passed
to `create_chat_agent` by the application configuration.

The backend still needs to implement `ChatReadPort`, configure it in the worker
before polling starts, register the agent with the Pydantic AI Temporal plugin,
and add the chat API, workflow, SSE relay, persistence, approval, and write
activities. The port is deliberately read-only. A `ChangeProposal` is not an
executable command.

`ChatDeps` contains only the program and version IDs so Pydantic AI can serialize
it across Temporal activity boundaries. Do not add a database pool, repository,
HTTP client, or service instance to it.
