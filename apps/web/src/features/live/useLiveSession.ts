import type { LiveSessionResponse, StreamLiveSessionEventsData } from "@livecart/api-client";
import { skipToken, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { keys } from "../../api/queries";
import { useServices } from "../../app/services";
import { applyEvent, fromSnapshot, type LiveState } from "../../realtime/applyEvent";
import { openLiveStream, type StreamStatus } from "../../realtime/sseClient";

/**
 * A session's live state, kept in the query cache (`["live", id]`) and
 * written by its event stream: the snapshot replaces it, each event goes
 * through `applyEvent`. The session's own entry follows its status, so the
 * other pages read what the stream said. Other pages read the cache too: the
 * order page finds an order's shipment there.
 */
export function useLiveSession(sessionId: string): {
  state: LiveState | undefined;
  status: StreamStatus;
  /** Events were lost (stream.gap): the state is stale until the snapshot that follows. */
  catchingUp: boolean;
} {
  const { client, session, send } = useServices();
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const [catchingUp, setCatchingUp] = useState(false);
  // No fetch of its own: the stream writes this entry.
  const live = useQuery<LiveState>({
    queryKey: keys.live(sessionId),
    queryFn: skipToken,
    staleTime: Infinity,
  });

  useEffect(() => {
    const liveKey = keys.live(sessionId);
    const followSession = (updated: LiveSessionResponse) => {
      queryClient.setQueryData(keys.session(sessionId), updated);
    };
    const stream = openLiveStream({
      url: client.buildUrl<StreamLiveSessionEventsData>({
        url: "/api/v1/live-sessions/{session_id}/events",
        path: { session_id: sessionId },
      }),
      send,
      auth: session,
      handlers: {
        onSnapshot: (snapshot) => {
          const state = fromSnapshot(snapshot);
          queryClient.setQueryData(liveKey, state);
          followSession(state.session);
          setCatchingUp(false);
        },
        onEvent: (event) => {
          const before = queryClient.getQueryData<LiveState>(liveKey);
          if (before === undefined) {
            return;
          }
          const after = applyEvent(before, event);
          if (after === before) {
            return;
          }
          queryClient.setQueryData(liveKey, after);
          if (after.session !== before.session) {
            followSession(after.session);
            void queryClient.invalidateQueries({ queryKey: keys.sessions });
          }
        },
        onGap: () => {
          setCatchingUp(true);
        },
        onStatus: setStatus,
      },
    });
    return () => {
      stream.close();
    };
  }, [client, queryClient, send, session, sessionId]);

  return { state: live.data, status, catchingUp };
}
