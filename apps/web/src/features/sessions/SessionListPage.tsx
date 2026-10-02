import { PageTitle } from "../../ui/feedback";

/** `/sessions`: the seller's live sessions (the list and its form come next). */
export function SessionListPage() {
  return (
    <section>
      <PageTitle title="Live sessions">Live sessions</PageTitle>
      <p className="mt-2 text-sm text-ink-muted">Your live sessions will be listed here.</p>
    </section>
  );
}
