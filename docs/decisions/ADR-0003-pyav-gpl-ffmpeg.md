# ADR-0003: Accept PyAV's GPL-built FFmpeg for server-side audio decoding

- **Status:** Accepted (13 Nov 2026, V2 slice 1).
- **Deciders:** project owner

## Context

V2 speech-to-text needs to decode what browsers record (Chrome/Firefox: webm/Opus; Safari: mp4/AAC) into
16 kHz mono for Whisper. We use **PyAV** (`av` 18.1.0), which decodes in memory, so audio never touches disk
(plan §6.6). The plan's local STT engine, **faster-whisper** (MIT), also requires PyAV as a hard dependency.

PyAV's own code is BSD-3-Clause, but its PyPI wheels bundle FFmpeg built with GPL-only components
(`libx264`, `libx265` are present in `av.codecs_available`). So the bundled FFmpeg binaries are **GPL**. We
never use those video encoders, but the licence follows the binary as built.

CLAUDE.md rule 3 bans gated, non-commercial or GPL **models**. That rule exists because a model's licence
governs what can be done with the product's output. A decoding library produces no output anyone sees.

StoryWeaver is a portfolio project. It is **never distributed**: no shipped app, installer or published
Docker image. It runs only as our own server (the HF Space) and our local Docker stack.

## Decision

Accept PyAV with its GPL-built FFmpeg, server-side only.

- GPL obligations (offering source, GPL-compatible licensing of the combined work) are triggered by
  **distribution**. Running the software on our own server is not distribution, and FFmpeg is not AGPL.
- The repositories (GitHub and the Space) contain our code and a requirements list. pip downloads PyAV from
  PyPI. We don't redistribute its binaries.

## Alternatives considered

- **Drop faster-whisper** (Groq Whisper only). This removes PyAV, but loses `STT_ENGINE=local` (voice kept on
  our server, plan §6.6) and the fallback required by architecture rule 2. Voice input would then fail
  whenever Groq quota runs out. Rejected.
- **Browser sends WAV; decode with the stdlib `wave` module.** This removes our own use of FFmpeg, but
  faster-whisper still installs PyAV, so the GPL binary remains. It also costs extra frontend work.
  Rejected.
- **Build FFmpeg as LGPL.** This would mean maintaining custom wheels or a custom image. It is
  disproportionate for a project that is never distributed. Rejected.

## Consequences

- No obligations today, and no cost: every component is free.
- **Revisit before any distribution** (a desktop or mobile app, a published Docker image, or selling the
  software). Then either comply with the GPL or switch to an LGPL FFmpeg build.
- Model licences are unaffected and still recorded per model. faster-whisper `small` (Systran) is MIT.
