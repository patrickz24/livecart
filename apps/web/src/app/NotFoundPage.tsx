import { Link } from "react-router";

import { PageTitle } from "../ui/feedback";

/** Any path the dashboard does not know. */
export function NotFoundPage() {
  return (
    <main className="mx-auto max-w-xl p-8">
      <PageTitle title="Page not found">Page not found</PageTitle>
      <p className="mt-2 text-sm text-ink-muted">
        This page does not exist.{" "}
        <Link to="/sessions" className="font-medium text-brand underline">
          Back to your sessions
        </Link>
      </p>
    </main>
  );
}
