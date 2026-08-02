---
inclusion: always
---

# Writing Style

Content in this repo has two audiences: humans and machines. Match the style
to the audience.

## Human-Facing Documents and Conversations

Human readers want to understand and move on. Write for that.

- Use short sentences.
- Lead with the point.
- Prefer plain language and concrete examples.
- Prefer bullet points or simple statements whereever possible.
- Keep paragraphs to 2–4 sentences.
- Use bullet lists for enumerations and steps.
- Show through code snippets and command examples wherever they carry the point faster than prose.

### ASCII Diagrams

Humans read pictures faster than paragraphs. When a document explains how a
system works, the shape of an interface, or the steps of a complex process,
an ASCII diagram is often the fastest way for a person to understand it —
often preferred over a lengthy technical description of the same thing.

Reach for an ASCII diagram when:

- The reader is asking "how does X work?" and the answer has moving parts.
- The topic is a multi-actor sequence — request → handler → downstream, or
  a state transition, or a lifecycle.
- The topic is an interface shape or component layout where relative
  positioning carries meaning.

Skip the diagram when the topic is simple enough that a sentence or two
covers it. A picture that adds no more information than the prose around it
is noise. Reserve diagrams for the moments where they save the reader real
effort.

Examples of human-facing documents: `README.md`, quickstart guides, tutorials,
onboarding pages, top-of-file docstrings for exported APIs.

Examples of human conversations:  live chat sessions

## Machine-Facing Documents

Steering files, agent instructions, and internal system documentation are read
by AI agents that benefit from thoroughness. Write for that.

- Cover the full behavior, including edge cases and cross-references.
- Use structured formats — tables, headings, front-matter.
- Spell out the "why" alongside the "what" wherever it affects agent decisions.
- Include canonical file paths and command examples so agents can act directly.

Examples of machine-facing documents: `.kiro/steering/*.md`,
`.kiro/powers/*/POWER.md`, `AGENTS.md`, `CLAUDE.md`.

### Mermaid Diagrams

Machines do well with mermaid diagrams when needed, they are compact and concise

## Universal Rule: Describe What Something Is

Write in the affirmative. State what a thing is, what it does, and what it
requires. Positive framing reads faster and leaves less room for
misinterpretation.

Preferred forms:

- "Commit to the package's own repo from `workplace/src/<PackageName>/`."
- "The `mainline` branch advances only through reviewed CRs."
- "`workplace/` is gitignored; the bootstrap creates and manages it."
- "The bootstrap script skips packages with local changes."

When a restriction is genuinely load-bearing, phrase it as an active
requirement rather than a negation. State the rule that holds.

## Chat and Agent Responses

Direct responses to the user follow the human-facing style: short, intuitive,
directly answering the question. Reserve completeness for documents the user
has asked to be written or for when they are requesting such detailed information.