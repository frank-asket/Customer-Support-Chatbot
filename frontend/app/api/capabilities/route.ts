import { NextResponse } from "next/server";

function getBackendBaseUrl() {
  const raw = process.env.BACKEND_API_URL;
  if (!raw) return null;
  const trimmed = raw.trim().replace(/\/$/, "");
  if (!trimmed) return null;
  return /^https?:\/\//i.test(trimmed) ? trimmed : `https://${trimmed}`;
}

export async function GET() {
  try {
    const backendApiUrl = getBackendBaseUrl();
    if (!backendApiUrl) {
      return NextResponse.json({ error: "Missing BACKEND_API_URL environment variable" }, { status: 500 });
    }

    const response = await fetch(`${backendApiUrl}/capabilities`, {
      method: "GET",
      headers: { "Content-Type": "application/json" },
      cache: "no-store"
    });
    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    return NextResponse.json(
      {
        error: "Capabilities fetch failed",
        details: error instanceof Error ? error.message : "Unexpected error"
      },
      { status: 500 }
    );
  }
}
