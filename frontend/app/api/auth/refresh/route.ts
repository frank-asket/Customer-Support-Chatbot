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
    const response = await fetch(`${backendApiUrl}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });

    const raw = await response.text();
    let data: Record<string, unknown>;
    try {
      data = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
    } catch {
      data = {
        error: response.ok ? "Unexpected non-JSON response" : "Backend refresh failed",
        details: raw.slice(0, 500) || response.statusText || "No response body"
      };
    }
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    return NextResponse.json(
      {
        error: "Session refresh failed",
        details: error instanceof Error ? error.message : "Unexpected error"
      },
      { status: 500 }
    );
  }
}
