// A Sentry DSN looks like https://<public key>@<host>/<project id>.
export type ParsedDsn = { host: string; projectId: string; publicKey: string };

export function parseDsn(dsn: string | undefined | null): ParsedDsn | null {
  if (!dsn) return null;
  try {
    const url = new URL(dsn);
    const projectId = url.pathname.replace(/^\/+|\/+$/g, "");
    if (url.protocol !== "https:" || !url.username || !/^\d+$/.test(projectId)) return null;
    return { host: url.host, projectId, publicKey: url.username };
  } catch {
    return null;
  }
}
