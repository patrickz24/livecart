import { endLiveSession, startLiveSession, type LiveSessionResponse } from "@livecart/api-client";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { ApiError, describeError, unwrap } from "../../api/errors";
import { keys } from "../../api/queries";
import { useServices } from "../../app/services";
import { Button } from "../../ui/Button";
import { Alert, focusPageTitle } from "../../ui/feedback";

type Move = "start" | "end";

function refusal(move: Move, error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    // The machine refused: the session moved meanwhile (another tab, another device).
    const why = error.detail ?? "Its status does not allow it.";
    return `The live could not ${move === "start" ? "start" : "end"}: ${why}. Its status is shown again as the server holds it.`;
  }
  return describeError(error);
}

/**
 * "Start live" on a draft, "End live" on a live session, along the API's
 * machine (draft, live, ended). Ending cannot be undone, so it asks first,
 * with the focus on the confirmation and Escape to cancel. A 409 means the
 * session moved meanwhile: the message says so and the session is read again.
 * Once a move settles, the focus goes to the page's heading whenever the
 * control pressed may be gone: the status changed or is read again (Start
 * live and End live go with it), or the confirmation closed.
 */
export function LifecycleControls({ session }: { session: LiveSessionResponse }) {
  const { client } = useServices();
  const queryClient = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const confirmButton = useRef<HTMLButtonElement>(null);
  const endButton = useRef<HTMLButtonElement>(null);

  const move = useMutation({
    mutationFn: (to: Move) => {
      const options = { client, path: { session_id: session.id } };
      return unwrap(to === "start" ? startLiveSession(options) : endLiveSession(options));
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(keys.session(session.id), updated);
      void queryClient.invalidateQueries({ queryKey: keys.sessions });
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        void queryClient.invalidateQueries({ queryKey: keys.session(session.id) });
      }
    },
    onSettled: (_updated, error, to) => {
      setConfirming(false);
      const reread = error instanceof ApiError && error.status === 409;
      if (error === null || reread || to === "end") {
        focusPageTitle();
      }
    },
  });

  useEffect(() => {
    if (confirming) {
      confirmButton.current?.focus();
    }
  }, [confirming]);

  function cancel() {
    setConfirming(false);
    endButton.current?.focus();
  }

  return (
    <div className="flex flex-col items-start gap-2">
      {session.status === "draft" ? (
        <Button
          variant="primary"
          disabled={move.isPending}
          onClick={() => {
            move.mutate("start");
          }}
        >
          {move.isPending ? "Starting…" : "Start live"}
        </Button>
      ) : null}

      {session.status === "live" ? (
        <Button
          ref={endButton}
          variant="danger"
          aria-expanded={confirming}
          aria-controls="end-live-confirmation"
          disabled={move.isPending}
          onClick={() => {
            move.reset();
            setConfirming(true);
          }}
        >
          {move.isPending ? "Ending…" : "End live"}
        </Button>
      ) : null}

      {session.status === "live" && confirming ? (
        <div
          id="end-live-confirmation"
          role="group"
          aria-labelledby="end-live-question"
          className="rounded-control border border-danger bg-danger-soft p-3"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              cancel();
            }
          }}
        >
          <p id="end-live-question" className="text-sm text-danger">
            End this live? Comments stop reserving stock, and the session can no longer change.
          </p>
          <div className="mt-2 flex gap-2">
            <Button
              ref={confirmButton}
              variant="danger"
              disabled={move.isPending}
              onClick={() => {
                move.mutate("end");
              }}
            >
              End live now
            </Button>
            <Button onClick={cancel}>Keep it live</Button>
          </div>
        </div>
      ) : null}

      {move.isError ? <Alert>{refusal(move.variables, move.error)}</Alert> : null}
    </div>
  );
}
