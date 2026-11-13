"use client";

import * as Sentry from "@sentry/browser";
import { useEffect } from "react";

type Props = { dsn: string; environment: string; release: string };

let started = false;

/** Sends unexpected browser errors to Sentry through the app's own /monitoring endpoint.
 * Rendered only when the server has SENTRY_DSN_WEB. No session replay, no personal data:
 * cookies, request bodies and query strings are removed before anything is sent. */
export function ErrorReporting({ dsn, environment, release }: Props) {
  useEffect(() => {
    if (started) return;
    started = true;
    Sentry.init({
      dsn,
      environment,
      release,
      tunnel: "/monitoring",
      // Nothing about the person or their requests: no user fields, cookies, headers,
      // bodies or query strings.
      dataCollection: { userInfo: false, cookies: false, httpHeaders: false, httpBodies: [], urlQueryParams: false },
      tracesSampleRate: 0,
      beforeSend(event) {
        if (event.request) {
          delete event.request.cookies;
          delete event.request.data;
          if (event.request.url) event.request.url = event.request.url.split("?")[0];
          delete event.request.query_string;
        }
        return event;
      },
      beforeBreadcrumb(crumb) {
        // Keep that a request happened and its status, not its query string.
        if (crumb.data && typeof crumb.data.url === "string") crumb.data.url = crumb.data.url.split("?")[0];
        return crumb;
      },
    });
  }, [dsn, environment, release]);
  return null;
}

export function reportError(error: unknown) {
  if (started) Sentry.captureException(error);
}
