import { BASE } from "./api";

interface AuditEvent {
  action: string;
  target_type: string | null;
  target_id: string | null;
  metadata: Record<string, unknown> | null;
}

// Shared in-memory buffer — survives across route changes within the SPA.
const _buffer: AuditEvent[] = [];
const _MAX_BUFFER = 20;

function _token(): string {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("tt_token") ?? "";
}

function _endpoint(): string {
  return `${BASE}/v1/audit/events`;
}

/**
 * Queue a UI event for background delivery.
 * Returns immediately — never throws.
 */
export function logEvent(
  action: string,
  targetType?: string,
  targetId?: string,
  metadata?: Record<string, unknown>,
): void {
  _buffer.push({
    action,
    target_type: targetType ?? null,
    target_id: targetId ?? null,
    metadata: metadata ?? null,
  });
  if (_buffer.length >= _MAX_BUFFER) {
    void flush();
  }
}

/**
 * Drain the buffer with a normal async fetch.
 * Called by the periodic timer in AuditTracker.
 */
export async function flush(): Promise<void> {
  if (_buffer.length === 0) return;
  const token = _token();
  if (!token) {
    _buffer.length = 0;
    return;
  }
  const batch = _buffer.splice(0, _buffer.length);
  try {
    await fetch(_endpoint(), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(batch),
    });
  } catch {
    // best-effort — drop on error, never retry
  }
}

/**
 * Drain the buffer using a keepalive fetch so events survive page navigation
 * or the document going hidden. Called on visibilitychange → hidden.
 */
export function flushOnHide(): void {
  if (_buffer.length === 0) return;
  const token = _token();
  if (!token) { _buffer.length = 0; return; }
  const batch = _buffer.splice(0, _buffer.length);
  fetch(_endpoint(), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify(batch),
    keepalive: true,
  }).catch(() => {});
}
