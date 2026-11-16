import { SignIn } from "@/components/SignIn";
import { StoryViewer } from "@/components/StoryViewer";
import { currentSession } from "@/lib/session";

export const dynamic = "force-dynamic";

/** The owner's own story. Anyone else (signed in or not) can only read it through a share link: /s/{token}. */
export default async function StoryPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const session = await currentSession();
  return <main>{session ? <StoryViewer id={id} /> : <SignIn next={`/story/${id}`} />}</main>;
}
