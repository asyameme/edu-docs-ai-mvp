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

## Edit loop (approved proposals)

`proposal_apply.py` is the bridge between an approved `ChangeProposal` and the
two effects of an edit: the next graph version and the DOCX patch inputs.

- `plan_approved_changes(snapshot, proposal)` validates the whole proposal
  against the confirmed snapshot before anything is applied: the target exists,
  the field is present, `expected_old_value` still equals the current value
  (staleness), every `source_ref_id` belongs to the recorded field evidence
  (`source_links`). All problems are collected into one rejection message.
- `apply_change(snapshot, change)` returns a new snapshot with one edit applied;
  the previous snapshot is never mutated. One change = one version created by
  the backend (`VersionCreate` with `expected_version_id`), so concurrent edits
  fail on optimistic locking and go to conflict resolution.
- `generation_inputs(snapshot, changes)` produces the exact
  `build_generation_documents` input: a fact recorded in several documents is
  patched in all of them. `touched_document_ids` reports the affected documents
  before anything is applied.
- Agent-facing `field_path` is the attribute name (`hours`); it is normalized
  to the JSON Pointer form (`/attributes/hours`) used by the evidence and the
  generation layer. Whole-object (`$`) and non-attribute paths are rejected:
  they cannot be mapped to document text.

`write_port.py` declares `ChatWritePort` — the contract the backend implements
in Temporal: apply changes one by one, create a version per change, run the
rules after EVERY change, return findings with the result. `validation_adapter.py`
maps a `run_validation` report into `validation_issues`-shaped records so
post-edit checks land in the same table the chat already reads.

Only attribute writes are mapped to text replacement; adding or removing
entities changes document structure and stays out of scope.
