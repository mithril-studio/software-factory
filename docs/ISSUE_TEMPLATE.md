# The issue template

The issue body is the agent's whole brief. Four sections make the difference between a pull
request you can merge and one you have to explain. Copy this into a target repo as
`.github/ISSUE_TEMPLATE/factory.md` to get it from GitHub's "New issue" button.

```markdown
---
name: Factory task
about: Work for the software factory. Label agent:queued when it is ready.
---

## What and why

One paragraph: the change, and the reason for it. Name the files or screens it touches if you
know them.

## Acceptance criteria

- [ ] A thing a reviewer can check is true, phrased as an outcome
- [ ] Another one
- [ ] Tests for the new behaviour pass (name the command if it is not the usual one)

## Out of scope

- What the agent must not touch or "improve" while it is there

## How to verify

The commands or steps that prove it works, e.g. `npm test`, or "open /settings and save".
```

What each part buys:

- **What and why** stops the agent solving a different problem well.
- **Acceptance criteria** are the definition of done. The agent is told every one must be true;
  you review against the same list.
- **Out of scope** is the cheapest guard against a sprawling diff.
- **How to verify** is what the agent runs before it opens the pull request.

Small issues go better than large ones. If the criteria list passes about six items, it is
probably two issues; with `FACTORY_SEQUENTIAL` on they are built in order, each on top of the
last.
