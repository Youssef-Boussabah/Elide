import { useCallback, useEffect, useState } from "react";
import { api, explain } from "../api/client";

export type Resource<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
};

type Result<T> = { key: string | null; data: T | null; error: string | null };

/** GET a JSON resource; reloads when `path` changes or `reload()` is called. The previous
 * data stays visible while a reload is in flight. */
export function useResource<T>(path: string | null): Resource<T> {
  const [tick, setTick] = useState(0);
  const [result, setResult] = useState<Result<T>>({ key: null, data: null, error: null });
  const key = path === null ? null : `${path}#${tick}`;

  useEffect(() => {
    if (path === null || key === null) return;
    let live = true;
    api<T>(path)
      .then((data) => live && setResult({ key, data, error: null }))
      .catch((e: unknown) => live && setResult((r) => ({ key, data: r.data, error: explain(e) })));
    return () => {
      live = false;
    };
  }, [path, key]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  return {
    data: result.data,
    error: result.key === key ? result.error : null,
    loading: key !== null && result.key !== key,
    reload,
  };
}
