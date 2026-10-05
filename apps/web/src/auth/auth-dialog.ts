/** Compatibility entry: authentication now owns a standalone page. */
import type { MeResponse } from "./api";

export function openAuthDialog(options: { onAuthenticated?: (me: MeResponse) => void } = {}): void {
  void options;
  if (window.location.pathname.replace(/\/$/, "") === "/login") return;
  const next = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
}
