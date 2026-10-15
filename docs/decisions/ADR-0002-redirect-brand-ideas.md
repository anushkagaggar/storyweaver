# ADR-0002: Story ideas that name brands are adapted, not refused

- **Status:** Accepted (16 Oct 2026, V1.1).
- **Deciders:** project owner

## Context

In V1 the policy model refused any idea naming a brand or trademarked character. Live, "Tom and Jerry" was
refused with the same message as a violent idea ("Try a gentle, kind idea."), which misled the parent: the
idea was not unkind, it only used characters we can't reproduce. Every refusal showed that one message,
although the backend knew the category.

We still must not publish stories (or, from V3, pictures) of trademarked characters.

## Decision

1. **Brands redirect.** In a story idea, a brand is not a violation. The policy model returns a separate
   `brand_mentioned` flag; the idea is accepted with `stories.adaptation = "brands"` (migration 0002).
   Any other violation in the same idea still refuses it ("Tom and Jerry hit each other with pans" →
   violence).
2. **The planner invents original characters.** For an adapted idea it gets an explicit instruction, and
   `story_problems()` rejects any character name that reuses a word from the idea, which goes through the
   normal single repair call.
3. **The output check is unchanged in strength.** In a finished story, a brand is still a violation, so a
   story that kept a trademarked name fails closed.
4. **The reader is told.** The story carries a `notice` ("we made up our own characters inspired by it"),
   shown above the book and passed through MCP.
5. **Every other refusal explains itself.** Each blocked category has its own message saying why and what to
   try instead (`INPUT_CATEGORY_MESSAGES`). Real people stay refused.

## Verification (live, Groq free tier, 16 Oct 2026)

- Input guard on 7 ideas: 3 brand ideas adapted; violence, real people and scary refused with their own
  messages; a safe idea allowed. All matched the expected result.
- "a story about Tom and Jerry" was planned as "Pip and Nix's Moonlit Adventure" (no borrowed names), and
  the output check passed it.

## Consequences

- Friendlier for parents; nothing trademarked reaches a reader.
- The policy now has a second output field; its size stays within Groq's recommended 400–650 tokens.
- The name check is a word match, so an adapted idea that also contains a child's first name ("Mia meets
  Peppa Pig") makes the planner rename Mia too. Accepted for now; revisit if it shows up in practice.
