import { NextResponse } from "next/server";

import { callBackend } from "@/lib/backend";
import { callGradioApi } from "@/lib/gradio";

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    // GPU check may queue on ZeroGPU; keep well under Vercel's function limit.
    const [ready, gpu, groq] = await Promise.all([
      callBackend("/health/ready", 15_000),
      callGradioApi("gpu_check", 90_000),
      callBackend("/v1/groq-check", 30_000),
    ]);
    return NextResponse.json({ backend: ready, gpu, groq });
  } catch (error) {
    console.error("Status check misconfigured", error);
    return NextResponse.json(
      { type: "about:blank", title: "Internal Server Error", status: 500, detail: "Server is misconfigured." },
      { status: 500, headers: { "content-type": "application/problem+json" } },
    );
  }
}
