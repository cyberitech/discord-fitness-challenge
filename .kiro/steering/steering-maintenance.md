---
inclusion: never
---

# Steering Maintenance Contract

Steering files in `.kiro/steering/` are load-bearing context. Every AI agent
that works on this codebase consumes them as authoritative truth about how
the system is built and how it must be changed. Stale or contradictory
steering produces wrong answers from every downstream agent that reads it,
compounding across every future task in the repo.

The rules below MUST be treated with the same rigor as compiling code. A
change that lands with drift between source and steering is defective, no
matter how green the build is.

Wherever RFC 2119 keywords appear (MUST, MUST NOT, SHOULD, SHOULD NOT, MAY),
they carry their RFC 2119 meaning.

## Create Steering Docs To Note Invariables And Architecture

As you build things and have things such as host names, arhcitecture, 
intended design, application intent, etc, save these as steering documents.

Be sure your steering documetns are not overlery narrow and do not assume 
information that the user did not ask for.



## Update Steering In The Same Change

When code, configuration, or infrastructure is written, produced, or
modified, the agent MUST identify every steering document whose content is
rendered inaccurate, incomplete, or misleading by that change, and MUST
update those documents in the same commit or code review that introduces
the code change.

"Same change" means:
- Same git commit for changes within a single package repo.
- Same CR / merge-request bundle for changes that span multiple packages.
- Same working session for local-only iterations.

Landing code first and "circling back to update the docs later" is
prohibited. There is no later. The gap between code changing and steering
catching up produces exactly the wrong-answer failure mode this contract
exists to prevent.

## Identify Affected Steering Before Writing Code

Before making a code change, the agent MUST search the steering tree for
any file that describes the affected behavior, interface, file layout,
dependency, or workflow. The search surface at minimum includes:

- `.kiro/steering/global/` — cross-package rules
- `.kiro/steering/service_metadata/` — architecture, components,
  interfaces, dependencies, workflows
- `.kiro/steering/<PackageName>/` — package-scoped steering
- Any `AGENTS.md` or `README.md` inside the affected package

If a matching steering file exists, the agent MUST plan the corresponding
update alongside the code change. Beginning the code change without that
plan in mind is prohibited.

## Create New Steering For New Components

When a new feature, component, subsystem, agent, Lambda handler, container,
CDK stack, or otherwise non-trivial capability is introduced, the agent
SHOULD create a steering document for it if any of the following holds:

- Future agents will need concept-level context (what it is, why it
  exists, how it fits into the larger system) that is not obvious from
  reading the code.
- The component has operational nuance (dev loops, mount paths, credential
  requirements, dependency ordering) that cannot be inferred from source.
- The component establishes conventions that other components in the same
  package or elsewhere are expected to follow.

New package-scoped steering MUST live under
`.kiro/steering/<PackageName>/<topic>.md` and SHOULD carry appropriate
front matter (`inclusion: fileMatch` with a `fileMatchPattern` scoped to
the package, or `inclusion: manual` for reference-only content).

When creating a new steering file does not make sense — for example, the
change is a natural extension of an existing subsystem — the agent MUST
instead extend the existing steering file that already covers that
subsystem. The agent MUST NOT leave the new capability undocumented in
the steering tree.

## Update, Do Not Duplicate

When the same fact is captured in multiple steering documents, the agent
MUST update every occurrence in the same change. Consolidating duplicated
facts into a single canonical file (with the others referencing it) is
preferred, but consolidation is a follow-up — the immediate obligation is
to leave every copy consistent.

The agent MUST NOT create a new steering document that re-states content
already covered by an existing one. If the existing document is in the
wrong place or has the wrong shape, the agent SHOULD refactor it rather
than shadow it.

## Deletes And Renames

When code, a component, or an interface is deleted or renamed, the agent
MUST remove or update every steering reference to it in the same change.
Dangling references to removed components are as harmful as stale
descriptions of live ones.

## Verification Before Landing

Before considering a code change complete, the agent MUST re-read the
steering files it touched (or should have touched) and confirm each of the
following:

1. Every fact stated in the updated steering matches the code that landed.
2. Every fact in the code that a future agent would need is either
   obvious from the source or captured in steering.
3. No steering file still describes the pre-change state as if it were
   current.
4. Cross-references between steering files still resolve.

If any of these fails, the change is incomplete.

## Escalation

If the agent identifies steering that appears wrong or contradictory but
is outside the scope of the current change, the agent MUST surface the
drift to the user in the same session rather than silently leaving it in
place. Silent tolerance of known-wrong steering violates this contract.
