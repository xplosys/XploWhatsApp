import { parseDsn } from "@/lib/sentryDsn";

// Browser error reports come here (same origin, so the page's CSP stays 'self' and ad
// blockers don't drop them) and are passed on to Sentry. Only envelopes for this app's
// own DSN are forwarded, so the endpoint can't be used to reach anything else.
const MAX_BYTES = 512 * 1024;

export async function POST(request: Request): Promise<Response> {
  const configured = parseDsn(process.env.SENTRY_DSN_WEB);
  if (!configured) return new Response(null, { status: 404 });
  const body = await request.text();
  if (body.length > MAX_BYTES) return new Response(null, { status: 413 });
  let header: { dsn?: string };
  try {
    header = JSON.parse(body.split("\n", 1)[0]);
  } catch {
    return new Response(null, { status: 400 });
  }
  const sent = parseDsn(header.dsn);
  if (!sent || sent.host !== configured.host || sent.projectId !== configured.projectId) {
    return new Response(null, { status: 400 });
  }
  try {
    const upstream = await fetch(`https://${configured.host}/api/${configured.projectId}/envelope/`, {
      method: "POST",
      headers: { "Content-Type": "application/x-sentry-envelope" },
      body,
      signal: AbortSignal.timeout(5000),
    });
    return new Response(null, { status: upstream.ok ? 200 : 502 });
  } catch {
    return new Response(null, { status: 502 });
  }
}
