import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = { title: "Privacy · StoryWeaver" };

// A short version of docs/privacy.md. Keep the two in step.
export default function PrivacyPage() {
  return (
    <main>
      <h1>Privacy</h1>
      <p className="lead">StoryWeaver is a portfolio project for parents to use with their children. It has no accounts.</p>

      <h2>Your voice</h2>
      <p>
        When you press &ldquo;Say your idea&rdquo;, your recording is turned into text and then thrown away.{" "}
        <strong>We never save it</strong>, and we don&apos;t write it to disk or to our logs. The text is made by Groq&apos;s
        speech service in the United States, which has Zero Data Retention switched on, or by a model on our own server.
      </p>

      <h2>Your story idea and the story</h2>
      <p>
        These are stored in our database (Neon, United States) for up to 30 days and sent to Groq to check they&apos;re suitable for children,
        write the story and read it aloud. Reading-voice audio and page pictures are kept in a private file store (Backblaze,
        United States); links to them expire after an hour. Nothing is used to train a model, sold, or used for adverts.
      </p>

      <h2>Story limits</h2>
      <p>
        So everyone gets a turn, each visitor can make a few stories an hour and a day. We count them under a scrambled code
        made from your internet address, which can&apos;t be turned back into the address, and forget the count within a day.
      </p>

      <h2>Who can read a story</h2>
      <p>
        Anyone who has the story&apos;s web address, which contains a long random code. Stories aren&apos;t listed anywhere, so
        only share the address with people you want to read it.
      </p>

      <h2>Please don&apos;t use real names</h2>
      <p>Don&apos;t type or say a child&apos;s name, address, school or anything else that identifies a real person.</p>

      <h2>Deleting a story</h2>
      <p>
        Every story has a &ldquo;Delete this story&rdquo; button that removes the idea, the pages, the pictures and the
        reading voice for good. Stories you don&apos;t delete are removed automatically 30 days after they were made.
      </p>

      <h2>Not yet</h2>
      <p>This project hasn&apos;t had a legal review and isn&apos;t offered to the public as a service.</p>

      <p className="again">
        <Link href="/">Back to making a story</Link>
      </p>
    </main>
  );
}
