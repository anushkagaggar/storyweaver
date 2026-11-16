import { MyStories } from "@/components/MyStories";
import { SignIn, SignOut } from "@/components/SignIn";
import { StoryForm } from "@/components/StoryForm";
import { currentSession } from "@/lib/session";

export const dynamic = "force-dynamic";

export default async function HomePage({ searchParams }: { searchParams: Promise<{ signin?: string }> }) {
  const session = await currentSession();
  const { signin } = await searchParams;
  return (
    <main>
      <h1>StoryWeaver</h1>
      <p className="lead">A gentle bedtime story, made just for tonight.</p>
      {session ? (
        <>
          <StoryForm />
          <MyStories />
          <SignOut />
        </>
      ) : (
        <SignIn reason={signin} />
      )}
    </main>
  );
}
