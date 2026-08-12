---
inclusion: never
---

# Project Name

ASF - Agile Security Framework


# Project Guidelines

Guidance that applies to every package in this service. Individual packages
carry their own steering (see the `fileMatch`-scoped files in this directory);
these are the rules that hold across all of them.

Wherever RFC 2119 keywords appear (MUST, MUST NOT, SHOULD, SHOULD NOT, MAY),
they carry their RFC 2119 meaning.

## Working With The User

The agent MUST use plain, short, direct statements when interacting with the
user. Long technical narratives and wordy paragraphs are reserved for cases
where the user explicitly asks for depth.

The agent SHOULD lead with the intuitive point and follow with detail only
when detail is needed. Show, don't over-explain.

The agent MUST think through non-trivial changes before acting and MUST
present the plan for user approval before enacting it. Non-trivial includes
new features, refactors, architectural changes, and edits that span multiple
packages.

The agent SHOULD ask clarifying questions when the request is abstract,
edge-case heavy, or commits the codebase to a load-bearing decision.
Small, obviously-scoped edits (renames, typo fixes, single-file tweaks) MAY
proceed without asking.

The agent MUST NOT ever take action on its own without first 
presenting its plans to the user and asking for feedback or approval

The agent SHOULD take direct action without asking when the user has
explicitly said to act immediately.

## Component Reuse First

Before writing new code, the agent MUST search the codebase for existing
functionality that could be extended or reused. Duplicate implementations
of the same idea across packages are a project smell.

The agent SHOULD extend existing components in preference to creating
parallel new ones. Consolidation is preferred over duplication.

## Simplicity Over Complexity

The agent SHOULD ALWAYS consider the readability benefits of 
writing code as single functions rather than splitting up functions 
over dozens of little helpers invoked by a central caller.

The agent SHOULD prefer to use helpers only when the helper is 
also used by other components.

The agent SHOULD NEVER write one line functions that wrap other 
functions unless it truly cannot be avoided

## Breaking Changes Within the Project

This is an internal service with no external consumers, however, code between packages may be dependent. 
Function signatures, class interfaces, and internal APIs MAY be modified freely PROVIDED that
every consumer within this project's packages is updated in the same change.

The agent SHOULD prefer aggressive refactoring and consolidation over
parallel duplicated implementations.

## Never Ask to Start Spec Session Without User First Requesting It

You MUST NOT ever attempt to start a spec session with a user unless they request it first.  
If you are in Vibe or Chat mode you SHOULD remain there unless otherwise requested by user.

## Fail Fast

Exception handling MUST be reserved for cases where recovery is genuinely
possible or where a specific transient failure warrants a retry. Wrapping
arbitrary code in `try/except` (or the language equivalent) is prohibited.

Agents MUST NOT ever use a catch-all exception on pieces of critical code.  If
the success of a function is required for the correct operation of the program, 
the agent must allow it to fail and produce a stack trace so that it can be debugged 
easier.

Agents MAY use exception handling when appriorate for control flow purposes 
such was when certain library calls are expected to fail in specific circumstances 
and the agent expects this may happen and falls back to another method of completing 
the task correctly.

See `general-logging-standards.md` for the required error-logging pattern,
including when a stack trace MUST be captured and when it MUST NOT.

## Function Structure

The agent SHOULD prefer single cohesive functions that read top-to-bottom
over chains of tiny helpers. A helper SHOULD be extracted only when it is
called from multiple sites or when it materially clarifies the surrounding
code. Single-use logic MUST be inlined.

## Code Quality

Python code MUST pass `flake8`, `mypy`, `isort`, and `black`. TypeScript
code MUST pass its package's configured linter and type checker. Code
SHOULD conform on the first pass; the agent MUST NOT lower lint standards
to make a build succeed.

Type hints MUST be present in Python wherever `mypy` requires them.

## Code Review Destination

All approved code reviews MUST target `mainline` as the destination branch.
Local work MAY use short-lived personal branches; the remote destination is
always `mainline`.

The agent MUST NOT create long-lived feature branches on the remote for
approved work. Unfinished work SHOULD stay uncommitted locally or land
behind a feature gate.

The agent MUST NOT force-push to `mainline`.

## Package Boundaries

Each package under `workplace/src/<PackageName>/` is its own git repo. The
agent MUST make separate commits per package when a change spans multiple
packages, and MUST make those commits from within the package's own
directory against its own remote.
