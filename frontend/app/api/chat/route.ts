import { NextResponse } from "next/server";

function getBackendBaseUrl() {
  const raw = process.env.BACKEND_API_URL;
  if (!raw) return null;
  const trimmed = raw.trim().replace(/\/$/, "");
  if (!trimmed) return null;
  return /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
}

export async function POST(req: Request) {
  try {
    const backendApiUrl = getBackendBaseUrl();
    if (!backendApiUrl) {
      return NextResponse.json({ error: "Missing BACKEND_API_URL environment variable" }, { status: 500 });
    }

    const body = await req.json();
    const response = await fetch(`${backendApiUrl}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    return NextResponse.json(
      {
        error: "Chat processing failed",
        details: error instanceof Error ? error.message : "Unexpected error"
      },
      { status: 500 }
    );
  }
}
