/** Display name for a device's raw platform string.
 *
 *  The agent reports raw `sys.platform` values ("darwin", "win32"), while other
 *  code paths store the friendlier "macos"/"windows" — so both spellings reach
 *  the dashboard and both have to map to the same label. Cosmetic only; nothing
 *  branches on the result.
 *
 *  Lives here because three device pickers now need it (Activity Logs, Reports,
 *  and the Technology & AI filter bar); it started as a local copy in the first
 *  two and a third copy was one too many. */
export function platformLabel(p: string): string {
  const v = p.toLowerCase();
  if (v === "darwin" || v === "macos") return "macOS";
  if (v === "win32" || v === "windows") return "Windows";
  if (v === "linux") return "Linux";
  return p;
}
