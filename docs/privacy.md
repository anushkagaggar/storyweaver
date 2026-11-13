# Privacy notice

*Draft, 13 Nov 2026 (V2). Written for the parent who uses StoryWeaver. It describes what the code does today; the
reasoning and the open risks are in [`dpia.md`](dpia.md).*

StoryWeaver is a portfolio project run by one person. It is not a commercial service, has no accounts and is
meant to be used by a parent, not by a child alone. Please don't type or say a child's name, address, school or
anything else that identifies a real person.

## What happens to what you give us

| What | Where it goes | How long it is kept |
|---|---|---|
| **Your voice** (when you press "Say your idea") | Your browser sends it to our web server, which passes it to our story server. There it is turned into text by **Groq's Whisper** service (United States). If that service is busy or switched off, the same step runs on our own server instead. | **It is never saved.** It exists in memory only while it is turned into text, then it is gone. We don't write it to disk and we don't log it. |
| **Your story idea** (typed, or the text from your voice) | Stored in our database (Neon, United States). Sent to Groq (United States) to check it is suitable for children and to write the story. | Currently **kept until we delete it by hand**. There is no automatic deletion yet (see "What we can't promise"). |
| **The story** (title, pages, characters) | Stored in our database. Sent to Groq to check it again before you see it, and to read it aloud when Groq's voice is used. | Same as the idea. |
| **The reading voice** (one audio file per page) | Made by Groq's Orpheus voice (United States), or by a voice model running on our own server. Stored in a private bucket at Backblaze (United States). | Kept with the story. Playback links expire after one hour. |
| **Usage counts** | Upstash Redis (United States): numbers that keep us inside free usage limits. | Minutes to a day. They contain no text, audio or names. |

We don't use your data to train any model. We don't sell it, show adverts or build profiles.

## Who can see a story

There are no accounts. A story is shown to anyone who has its web address, which contains a long random code.
Don't share the address unless you want others to read the story. Stories aren't listed anywhere.

## Who else is involved

Every service below is a company in the United States, so your data leaves your country when you use StoryWeaver.

- **Groq**: writes, checks and reads the story, and (usually) turns your voice into text. Groq offers a
  *Zero Data Retention* setting that stops it keeping what we send. **It has not been confirmed as switched on
  for this project**, so assume Groq may keep requests for its standard period (Groq's documentation mentions
  up to 30 days for reliability and abuse monitoring). See [Groq's data page](https://console.groq.com/docs/your-data).
- **Vercel** hosts the website and **Hugging Face** hosts the story server. Their systems keep routine logs of
  requests (time, address, result). They don't keep what you said or typed on our behalf.
- **Neon** (database), **Backblaze** (audio files) and **Upstash** (usage counts), as in the table.
- **Langfuse** (a debugging tool) is off unless switched on. Even when on, it records timings and sizes, not
  your words, unless a setting that is off by default is turned on.

## Keeping voice on our server

If you prefer your voice never to leave our own server, the site owner can set `STT_ENGINE=local`. Then voice
input is transcribed only by a model running on the story server, and Groq never receives a recording. (Typed
ideas and stories still go to Groq.)

## What we can't promise yet

- **Groq's retention.** As above: not confirmed as zero.
- **Deleting a story.** There is no delete button or automatic expiry yet. To have a story removed, contact the
  site owner with its web address.
- **No legal review.** This project hasn't been reviewed by a lawyer and is not offered to the public as a
  service. Children's privacy law (the US COPPA rules, which since 2025 treat voiceprints as personal
  information, and the UK Children's Code) would apply to a real launch.

## Contact

Open an issue at <https://github.com/anushkagaggar/storyweaver> or contact the repository owner.
