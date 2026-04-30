import { NextResponse } from "next/server";

export async function POST(req: Request) {
  try {
    const backendApiUrl = process.env.BACKEND_API_URL;
    if (!backendApiUrl) {
      return NextResponse.json({ error: "Missing BACKEND_API_URL environment variable" }, { status: 500 });
    }

    const body = await req.json();
    const response = await fetch(`${backendApiUrl.replace(/\/$/, "")}/auth/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });

    const data = await response.json();
    return NextResponse.json(data, { status: response.status });
  } catch (error) {
    return NextResponse.json(
      {
        error: "Verification failed",
        details: error instanceof Error ? error.message : "Unexpected error"
      },
      { status: 500 }
    );
  }
}
