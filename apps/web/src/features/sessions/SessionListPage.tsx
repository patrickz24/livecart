import { listLiveSessions } from "@livecart/api-client";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { describeError, unwrap } from "../../api/errors";
import { keys } from "../../api/queries";
import { useServices } from "../../app/services";
import { PLATFORM_LABELS, SESSION_STATUS } from "../../strings";
import { Button, ButtonLink } from "../../ui/Button";
import { Alert, Badge, Loading, PageTitle } from "../../ui/feedback";
import { formatDateTime } from "../../ui/format";

const PAGE_SIZE = 20;

/** `/sessions`: the seller's live sessions, newest first, a page at a time. */
export function SessionListPage() {
  const { client } = useServices();
  const sessions = useInfiniteQuery({
    queryKey: keys.sessions,
    queryFn: ({ pageParam, signal }) =>
      unwrap(listLiveSessions({ client, query: { limit: PAGE_SIZE, cursor: pageParam }, signal })),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
  });
  const items = sessions.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <section>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <PageTitle title="Live sessions">Live sessions</PageTitle>
        <ButtonLink to="/sessions/new" variant="primary">
          New session
        </ButtonLink>
      </div>

      {sessions.isPending ? <Loading /> : null}
      {sessions.isError ? <Alert className="mt-4">{describeError(sessions.error)}</Alert> : null}
      {sessions.isSuccess && items.length === 0 ? (
        <p className="mt-6 text-sm text-ink-muted">
          No live sessions yet. Create one, add its products, then start it when the live begins.
        </p>
      ) : null}

      {items.length > 0 ? (
        <div className="mt-6 overflow-x-auto rounded-control border border-line bg-surface">
          <table className="w-full text-left text-sm">
            <caption className="sr-only">Your live sessions, newest first</caption>
            <thead className="border-b border-line text-xs text-ink-muted uppercase">
              <tr>
                <th scope="col" className="px-4 py-3 font-medium">
                  Title
                </th>
                <th scope="col" className="px-4 py-3 font-medium">
                  Platform
                </th>
                <th scope="col" className="px-4 py-3 font-medium">
                  Status
                </th>
                <th scope="col" className="px-4 py-3 font-medium">
                  Created
                </th>
                <th scope="col" className="px-4 py-3 font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((session) => (
                <tr key={session.id} className="border-b border-line last:border-0">
                  <th scope="row" className="px-4 py-3 font-medium">
                    {session.title}
                    <span className="block text-xs font-normal text-ink-muted">
                      {session.external_live_id}
                    </span>
                  </th>
                  <td className="px-4 py-3">{PLATFORM_LABELS[session.platform]}</td>
                  <td className="px-4 py-3">
                    <Badge tone={SESSION_STATUS[session.status].tone}>
                      {SESSION_STATUS[session.status].label}
                    </Badge>
                  </td>
                  <td className="px-4 py-3 text-ink-muted">{formatDateTime(session.created_at)}</td>
                  <td className="px-4 py-3 text-right">
                    <Link
                      to={`/sessions/${session.id}/edit`}
                      className="font-medium text-brand underline"
                      aria-label={`Edit ${session.title}`}
                    >
                      Edit
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {sessions.hasNextPage ? (
        <Button
          className="mt-4"
          disabled={sessions.isFetchingNextPage}
          onClick={() => void sessions.fetchNextPage()}
        >
          {sessions.isFetchingNextPage ? "Loading…" : "Show older sessions"}
        </Button>
      ) : null}
    </section>
  );
}
