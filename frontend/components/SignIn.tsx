import Link from "next/link";

/** The sign-in prompt for parents who aren't signed in. A plain link: sign-in is a full-page trip to Google. */
export function SignIn({ next = "/", reason }: { next?: string; reason?: string | null }) {
  return (
    <section className="sign-in" aria-labelledby="sign-in">
      <h2 id="sign-in">Sign in to make a story</h2>
      {reason === "cancelled" && <p className="hint">Sign-in was cancelled. You can try again whenever you like.</p>}
      {reason === "failed" && (
        <p role="alert" className="error">
          Sign-in didn&apos;t work. Please try again.
        </p>
      )}
      <p>
        Your stories are kept private to you. We ask Google only for a sign-in code: not your name, email or picture.
      </p>
      <a className="button" href={`/api/auth/signin?next=${encodeURIComponent(next)}`}>
        Sign in with Google
      </a>
      <p className="hint">
        <Link href="/privacy">How we look after your data</Link>
      </p>
    </section>
  );
}

/** Sign out: a POST form, so another site can't sign you out with a link. */
export function SignOut() {
  return (
    <form method="post" action="/api/auth/signout" className="sign-out">
      <button type="submit" className="link-button">
        Sign out
      </button>
    </form>
  );
}
