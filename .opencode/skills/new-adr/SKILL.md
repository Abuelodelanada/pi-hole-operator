---
name: new-adr
description: >-
  Use when creating, numbering, or revising an Architecture Decision Record in
  docs/adr/ — the house ADR format, the Proposed/Accepted lifecycle, how to
  record a blocking spike, and how to decide whether something belongs in an
  ADR, in docs/roadmap.md, in docs/snap-constraints.md, or in docs/BACKLOG.md.
  Load before writing any file under docs/adr/.
metadata:
  verified: "2026-08-07"
---

# Writing an ADR

## Where things live — get this right first

Putting content in the wrong document is the most common failure here, because
all four look like "documentation".

| Document | Holds | Test |
|---|---|---|
| `docs/adr/NNNN-*.md` | **A decision**, its alternatives, and its consequences. | Could we have reasonably chosen otherwise? If no, it is not a decision. |
| `docs/snap-constraints.md` | **Verified facts** about the workload. | Is this true regardless of what we decide? |
| `docs/roadmap.md` | **Sequencing and acceptance criteria.** | Does this answer "in what order" or "how do we know it's done"? |
| `docs/BACKLOG.md` | **Deferred work**, with a trigger to revisit. | Are we explicitly not doing this yet? |

Two consequences:

- **Do not repeat design rationale in the roadmap.** It links to the ADR. If you
  find yourself explaining *why* in `roadmap.md`, the text belongs in an ADR.
- **Do not restate workload facts in an ADR.** Cite them:
  `[snap-constraints §4.2](../snap-constraints.md)`. When the snap changes, the
  fact is updated in one place and the ADR stays traceable to what was true when
  it was decided.

An observation is not an ADR. A verified fact is not an ADR. **A choice between
options is an ADR.**

## Procedure

1. **Read the existing ADRs' titles** to find the next number and to check the
   decision is not already recorded:

   ```bash
   ls docs/adr/
   ```

2. **Create `docs/adr/NNNN-<kebab-case-title>.md`** using the template below.
   Zero-padded to four digits, sequential, never reused — a superseded ADR keeps
   its number and gets `**Status:** Superseded by ADR-NNNN`.

3. **Add backlinks.** Every ADR this one relates to should gain a `Related` entry
   pointing back. Cross-references that only go one direction rot.

4. **Update the other documents if the ADR changes them:**
   - Does it change sequencing or acceptance? → `docs/roadmap.md`.
   - Does it defer something? → add it to `docs/BACKLOG.md` **annotated with the
     ADR number**, matching the existing style.
   - Does it graduate something from the backlog? → remove it there.
   - Does it introduce a blocking spike? → add a row to the roadmap's
     *Open spikes* table pointing at the ADR section.

5. **Report the path and number.**

## Template

```markdown
# ADR-NNNN: <Title>

**Status:** Proposed
**Date:** <YYYY-MM-DD>
**Related:** [ADR-NNNN: <Title>](NNNN-slug.md), [Snap constraints reference](../snap-constraints.md)

---

## 1. Context

<The situation that forces a decision. State the problem, not the solution.
Include the evidence — cite source files with line numbers, or snap-constraints
sections. If a fact is unverified, label it NOT VERIFIED.>

---

## 2. Problem Breakdown

<Optional. Use when the problem has distinct facets that each constrain the
answer. Subsections with `###`. Skip for simple decisions.>

---

## 3. Approaches

### A. <Name>

<Description, with the concrete command or code shape.>

**Pros**
- ...

**Cons**
- ...

### B. <Name>

...

---

## 4. Recommendation

<Which one, and the single deciding factor. Name explicitly any approach that is
rejected-and-recorded so it does not get rediscovered as a shortcut.>

---

## 5. Design

<How it actually works. Code sketches, tables, key names, orderings. This is the
section charm-engineer implements from.>

---

## 6. Open spike

<Only if the decision cannot be finalised without empirical evidence. A table of
questions and what each one decides. State plainly that the ADR cannot move to
Accepted until they are answered.>

---

## 7. Future Work (Out of Scope)

<Named, with the trigger to revisit. Mirror each item into BACKLOG.md.>

---

## 8. Consequences

### Positive

- ...

### Negative

- ...
```

Renumber the sections to be contiguous when you omit optional ones.

## Rules for the content

**Status is `Proposed` until it is real.** Move to `Accepted` only when the
decision is settled *and* any spike in §6 has been answered, with the answers
recorded in that section. An ADR whose spike is open must say so in the Status
line itself:

```markdown
**Status:** Proposed — **contains an unresolved spike, see §6**
```

**Approaches need at least two, and the rejected ones stay.** An ADR listing one
option is not documenting a decision, it is documenting an instruction. Recording
why an approach was rejected is often the most valuable part of the file: it stops
someone re-proposing it in six months, or worse, finding it in upstream
documentation and assuming it is the sanctioned path.

**Negative consequences are mandatory and must be real.** If the Negative section
is weaker than the Positive one, the ADR is advocacy rather than a record. Name
what the choice costs: added code, permanent API surface, a dependency, a
divergence from upstream defaults, a wart an operator will hit.

**Cite evidence, distinguish verified from assumed.** `snapcraft.yaml:240`,
`hooks/configure:213`, a section link, or a URL. Where something was not verified,
write **NOT VERIFIED** — that distinction is load-bearing in this repo, and an ADR
that launders an assumption into a fact is worse than one that admits the gap.

**Write for the reader who disagrees.** The audience is a contributor six months
from now who thinks the decision was wrong. Give them the reasoning to argue
against, not a conclusion to accept.

**Keep prose lines readable.** Wrap around 80 characters. The repo enforces 72 for
Python docstrings and comments; markdown is not linted, but consistency helps
review diffs.

## Anti-patterns

| Do not | Instead |
|---|---|
| Write an ADR for a decision with no alternative | It is a fact or an instruction — `snap-constraints.md` or the roadmap |
| Copy the `## Status` / `## Decision` heading form from generic ADR templates | Use the bold metadata block above; it is what the existing eight use |
| Number by date or by feature | Sequential integers, zero-padded to four |
| Edit an Accepted ADR to reflect a new decision | Write a new ADR and mark the old one `Superseded by ADR-NNNN`. The history is the point |
| Leave `Status: Proposed` on something already implemented | The status field is the only signal of what is settled; stale statuses make all of them worthless |
| Restate the snap's behaviour inline | Cite `snap-constraints.md` by section |
| Put implementation detail for code that exists in an ADR | That belongs in `docs/implementation/<module>.md` |

## `docs/implementation/` is a different thing

ADRs record decisions *before* code. `docs/implementation/<module>.md` documents
code that *exists*, with a header naming the module and linking back to the ADR
that decided it, then Purpose, Design, an Edge Cases table, and Testing Strategy.

Do not create one for a module that has not been written.
