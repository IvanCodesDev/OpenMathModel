/** Only return to this origin; reject protocol-relative, backslash and login loops. */
export function loginReturnPath(search: string, origin: string): string {
  const requested = new URLSearchParams(search).get("next") ?? "/";
  if (!requested.startsWith("/") || requested.startsWith("//") || requested.includes("\\") || [...requested].some(character => character.charCodeAt(0) < 32)) return "/";
  try {
    const target = new URL(requested, origin);
    if (target.origin !== origin || target.pathname.replace(/\/+$/, "") === "/login") return "/";
    return `${target.pathname}${target.search}${target.hash}`;
  } catch { return "/"; }
}
