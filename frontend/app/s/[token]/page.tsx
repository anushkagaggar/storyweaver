import type { Metadata } from "next";

import { StoryViewer } from "@/components/StoryViewer";

export const dynamic = "force-dynamic";
// The link is a credential for 15 minutes: keep it out of search engines and other sites' Referer headers.
export const metadata: Metadata = { robots: { index: false, follow: false }, referrer: "no-referrer" };

/** A story shared by its owner. No sign-in needed; it stops working 15 minutes after the link was made. */
export default async function SharedStoryPage({ params }: { params: Promise<{ token: string }> }) {
  const { token } = await params;
  return (
    <main>
      <StoryViewer shareToken={token} />
    </main>
  );
}
