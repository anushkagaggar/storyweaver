# Privacy notice

*Draft, 13 Nov 2026 (V2); pictures and story limits added 15 Nov 2026, sign-in and sharing 16 Nov 2026 (V3). The short in-app version is the `/privacy` page; keep the two in step. Written for the parent who uses StoryWeaver. It describes what the code does today; the
reasoning and the open risks are in [`dpia.md`](dpia.md).*

StoryWeaver is a portfolio project run by one person. It is not a commercial service and is meant to be used by
a parent, not by a child alone. You sign in with Google so that your stories are private to you. Please don't type or say a child's name, address, school or
anything else that identifies a real person.

## What happens to what you give us

| What | Where it goes | How long it is kept |
|---|---|---|
| **Your voice** (when you press "Say your idea") | Your browser sends it to our web server, which passes it to our story server. There it is turned into text by **Groq's Whisper** service (United States). If that service is busy or switched off, the same step runs on our own server instead. | **It is never saved.** It exists in memory only while it is turned into text, then it is gone. We don't write it to disk and we don't log it. |
| **Your story idea** (typed, or the text from your voice) | Stored in our database (Neon, United States) with your scrambled id. Sent to Groq (United States) to check it is suitable for children and to write the story. | **Deleted automatically 30 days after the story was made**, or straight away when you press "Delete this story". |
| **The story** (title, pages, characters) | Stored in our database. Sent to Groq to check it again before you see it, and to read it aloud when Groq's voice is used. | Same as the idea. |
| **The reading voice** (one audio file per page) | Made by Groq's Orpheus voice (United States), or by a voice model running on our own server. Stored in a private bucket at Backblaze (United States). | Deleted with the story. Playback links expire after one hour. |
| **The pictures** (one per page) | Drawn on our own story server, from the page's picture description. Stored in the same private Backblaze bucket. | Deleted with the story. Links expire after one hour (on a shared page, when the share link does). |
| **Signing in** | You sign in at **Google** (United States). We ask Google only for a sign-in code, **not your email, name or picture**. Our web server turns that code into a scrambled id that can't be turned back into your Google account, and stores only the scrambled id with your stories (Neon). | The id stays with each story until the story is deleted. |
| **The sign-in cookie** | One cookie in your browser that keeps you signed in. It holds only the scrambled id. We use no other cookies: no tracking or adverts. | 7 days, or until you press "Sign out". |
| **Usage counts** | Upstash Redis (United States): numbers that keep us inside free usage limits, including how many stories each person has made, counted under a further scrambled code. | Up to a day. They contain no text, audio, names, internet addresses or account details. |

We don't use your data to train any model. We don't sell it, show adverts or build profiles.

## Who can see a story

Only you, when you're signed in. Your stories are listed on the home page for you alone; nobody else can open,
list or delete them, even with the story's web address.

To show a story to someone else, press **"Share this story"**. That makes a read-only link that **works for 15
minutes from the moment you made it**. Anyone who has the link can read the story and listen to it until then,
without signing in. Opening or forwarding the link never makes it last longer, and the person you send it to
can't delete the story or make a new link. Make a new link whenever you want to share again.

## Deleting a story

Every story page has a **"Delete this story"** button. It removes the idea, the pages, the pictures and the
reading voice for good, and any share links stop working. Only you can do this. A story that is still being
made can be deleted once it is finished. Stories you don't delete are removed automatically **30 days** after they were made.

## Who else is involved

Every service below is a company in the United States, so your data leaves your country when you use StoryWeaver.

- **Groq**: writes, checks and reads the story, and (usually) turns your voice into text. Our Groq organisation has
  *Zero Data Retention* switched on (checked in Groq's Data Controls on 13 Nov 2026), so Groq doesn't log or keep
  what we send, instead of its standard period of up to 30 days. See [Groq's data page](https://console.groq.com/docs/your-data).
- **Google**: lets you sign in. Google learns that you signed in to StoryWeaver; we receive only a sign-in code
  (see the table), not your email, name or picture.
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
