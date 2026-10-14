import { StoryViewer } from "@/components/StoryViewer";

export default async function StoryPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return (
    <main>
      <StoryViewer id={id} />
    </main>
  );
}
