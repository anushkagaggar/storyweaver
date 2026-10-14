import { StoryForm } from "@/components/StoryForm";

export default function HomePage() {
  return (
    <main>
      <h1>StoryWeaver</h1>
      <p className="lead">A gentle bedtime story, made just for tonight.</p>
      <StoryForm />
    </main>
  );
}
