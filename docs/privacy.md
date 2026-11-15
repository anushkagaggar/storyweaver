# Privacy notice

*Draft, 13 Nov 2026 (V2); pictures and per-visitor limits added 15 Nov 2026 (V3). The short in-app version is the `/privacy` page; keep the two in step. Written for the parent who uses StoryWeaver. It describes what the code does today; the
reasoning and the open risks are in [`dpia.md`](dpia.md).*

StoryWeaver is a portfolio project run by one person. It is not a commercial service, has no accounts and is
meant to be used by a parent, not by a child alone. Please don't type or say a child's name, address, school or
anything else that identifies a real person.

## What happens to what you give us

| What | Where it goes | How long it is kept |
|---|---|---|
| **Your voice** (when you press "Say your idea") | Your browser sends it to our web server, which passes it to our story server. There it is turned into text by **Groq's Whisper** service (United States). If that service is busy or switched off, the same step runs on our own server instead. | **It is never saved.** It exists in memory only while it is turned into text, then it is gone. We don't write it to disk and we don't log it. |
| **Your story idea** (typed, or the text from your voice) | Stored in our database (Neon, United States). Sent to Groq (United States) to check it is suitable for children and to write the story. | **Deleted automatically 30 days after the story was made**, or straight away when you press "Delete this story". |
| **The story** (title, pages, characters) | Stored in our database. Sent to Groq to check it again before you see it, and to read it aloud when Groq's voice is used. | Same as the idea. |
| **The reading voice** (one audio file per page) | Made by Groq's Orpheus voice (United States), or by a voice model running on our own server. Stored in a private bucket at Backblaze (United States). | Deleted with the story. Playback links expire after one hour. |
| **The pictures** (one per page) | Drawn on our own story server, from the page's picture description. Stored in the same private Backblaze bucket. | Deleted with the story. Links expire after one hour. |
| **Usage counts** | Upstash Redis (United States): numbers that keep us inside free usage limits, including how many stories each visitor has made. A visitor is counted under a scrambled code made from their internet address, which can't be turned back into the address. | Up to a day. They contain no text, audio, names or internet addresses. |

We don't use your data to train any model. We don't sell it, show adverts or build profiles.

## Who can see a story

There are no accounts. A story is shown to anyone who has its web address, which contains a long random code.
Don't share the address unless you want others to read the story. Stories aren't listed anywhere.

## Deleting a story

Every story page has a **"Delete this story"** button. It removes the idea, the pages, the pictures and the
reading voice for good. Like reading, anyone with the story's address can do this. A story that is still being made can be deleted
once it is finished. Stories you don't delete are removed automatically **30 days** after they were made.

## Who else is involved

Every service below is a company in the United States, so your data leaves your country when you use StoryWeaver.

- **Groq**: writes, checks and reads the story, and (usually) turns your voice into text. Our Groq organisation has
  *Zero Data Retention* switched on (checked in Groq's Data Controls on 13 Nov 2026), so Groq doesn't log or keep
  what we send, instead of its standard period of up to 30 days. See [Groq's data page](https://console.groq.com/docs/your-data).
- **Vercel** hosts the website and **Hugging Face** hosts the story server. Their systems keep routine logs of
  requests (time, address, result). They don't keep what you said or typed on our behalf.
- **Neon** (database), **Backblaze** (audio and picture files) and **Upstash** (usage counts), as in the table.
- **Langfuse** (a debugging tool) is off unless switched on. Even when on, it records timings and sizes, not
  your words, unless a setting that is off by default is turned on.

## Keeping voice on our server

If you prefer your voice never to leave our own server, the site owner can set `STT_ENGINE=local`. Then voice
input is transcribed only by a model running on the story server, and Groq never receives a recording. (Typed
ideas and stories still go to Groq.)

## What we can't promise yet

- **No legal review.** This project hasn't been reviewed by a lawyer and is not offered to the public as a
  service. Children's privacy law (the US COPPA rules, which since 2025 treat voiceprints as personal
  information, and the UK Children's Code) would apply to a real launch.

## Contact

Open an issue at <https://github.com/anushkagaggar/storyweaver> or contact the repository owner.
