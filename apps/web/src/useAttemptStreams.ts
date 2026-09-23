import { useEffect, useRef, useState } from "react";

export function workspaceAttemptStreamUrl(attemptId: string): string {
  return `/api/v1/workspace/attempts/${attemptId}/stream`;
}

/**
 * Subscribe to the SSE stream of each active workspace attempt and expose the
 * latest partial answer per attempt id for typewriter rendering.
 *
 * Degrades gracefully: when EventSource is unavailable or the connection
 * errors, sources are closed and callers keep relying on their regular
 * polling, which stays enabled regardless.
 */
export function useAttemptStreams(
  attemptIds: string[],
  onDone: (attemptId: string) => void
): Record<string, string> {
  const [partials, setPartials] = useState<Record<string, string>>({});
  const onDoneRef = useRef(onDone);
  onDoneRef.current = onDone;
  const attemptKey = [...attemptIds].sort().join(",");

  useEffect(() => {
    const ids = attemptKey ? attemptKey.split(",") : [];
    if (!ids.length || typeof EventSource !== "function") return undefined;
    const sources: EventSource[] = [];
    for (const id of ids) {
      const source = new EventSource(workspaceAttemptStreamUrl(id));
      source.addEventListener("partial", (event) => {
        try {
          const data = JSON.parse((event as MessageEvent).data) as { text?: unknown };
          if (typeof data.text === "string" && data.text) {
            setPartials((current) => ({ ...current, [id]: data.text as string }));
          }
        } catch {
          // A malformed event is ignored; polling keeps the ledger consistent.
        }
      });
      source.addEventListener("done", () => {
        source.close();
        setPartials((current) => {
          const next = { ...current };
          delete next[id];
          return next;
        });
        onDoneRef.current(id);
      });
      source.onerror = () => {
        // Stream unavailable (network error or unsupported endpoint): close and
        // let the existing 1s polling deliver the final state instead.
        source.close();
      };
      sources.push(source);
    }
    return () => {
      for (const source of sources) source.close();
      setPartials((current) => {
        const next = { ...current };
        for (const id of ids) delete next[id];
        return next;
      });
    };
  }, [attemptKey]);

  return partials;
}
