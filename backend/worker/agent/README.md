# Chat agent module

This module contains the data-science-owned part of the chat: Pydantic AI
instructions, typed output, and read-only tool definitions. The model is passed
to `create_chat_agent` by the application configuration.

The backend adapter implements `ChatReadPort` against one immutable version and
configures it before polling starts. API and worker clients register the agent
through the Pydantic AI Temporal plugin. Chat sessions and turns are persisted
separately from Temporal execution history. The port is deliberately read-only.
A `ChangeProposal` is not an executable command.

`ChatDeps` contains only the program and version IDs so Pydantic AI can serialize
it across Temporal activity boundaries. Do not add a database pool, repository,
HTTP client, or service instance to it.
