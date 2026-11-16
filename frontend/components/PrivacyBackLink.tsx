"use client";

import Link from "next/link";
import { useSyncExternalStore } from "react";

const GOOGLE_SIGN_IN = "https://accounts.google.com";

function subscribeNever(): () => void {
  return () => {};
}

/**
 * True when this page was opened from Google's sign-in screen (its "Privacy Policy" link). Google sends its
 * origin as the referrer; `?from=google` also works, if the consent screen's privacy URL carries it.
 */
function cameFromGoogle(): boolean {
  return document.referrer.startsWith(GOOGLE_SIGN_IN) || new URLSearchParams(location.search).get("from") === "google";
}

function onServer(): boolean {
  return false;
}

/**
 * The way back from the privacy page. From Google's sign-in screen it goes back there (that sign-in is still
 * valid for 10 minutes); if Google opened us in a new tab, there's nothing to go back to, so it starts a fresh
 * sign-in instead. Otherwise it's the usual link home.
 */
export function PrivacyBackLink() {
  const fromGoogle = useSyncExternalStore(subscribeNever, cameFromGoogle, onServer);
  if (!fromGoogle) return <Link href="/">Back to making a story</Link>;
  return (
    <a
      className="button"
      href="/api/auth/signin"
      onClick={(event) => {
        if (history.length > 1) {
          event.preventDefault();
          history.back();
        }
      }}
    >
      Continue signing in
    </a>
  );
}
