/**
 * Cross-tab serialization for mutations of the shared HttpOnly refresh cookie.
 *
 * Web Locks is the primary path. The localStorage bakery lock is a compatibility
 * fallback and stores only short-lived coordination metadata, never credentials.
 */

const LOCK_NAME = 'map:browser-session-mutation';
const SLOT_PREFIX = 'map:browser-session-lock:';
const VERSION_KEY = 'map:browser-session-version';
const LEASE_MS = 60_000;
const HEARTBEAT_MS = 5_000;
const WAIT_TIMEOUT_MS = 250;

interface LockSlot {
  choosing: boolean;
  ticket: number;
  expiresAt: number;
}

export interface BrowserSessionVersion {
  revision: number;
  sequence: number;
  state: 'active' | 'cleared' | 'unknown';
  sessionId: string | null;
}

export interface BrowserSessionLockGuard {
  assertOwned: () => void;
}

interface StorageLockHandle extends BrowserSessionLockGuard {
  release: () => void;
}

let localQueue: Promise<void> = Promise.resolve();
let tabId: string | null = null;

function getTabId(): string {
  if (tabId) return tabId;
  const randomId = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  tabId = randomId;
  return randomId;
}

export function requireBrowserSessionStorage(): Storage {
  if (typeof window === 'undefined') {
    throw new Error('Browser session coordination is unavailable during SSR');
  }
  try {
    const storage = window.localStorage;
    const probe = `${SLOT_PREFIX}probe`;
    storage.setItem(probe, '1');
    storage.removeItem(probe);
    return storage;
  } catch {
    throw new Error(
      'Browser storage is required to coordinate a secure multi-tab session'
    );
  }
}

function parseSlot(raw: string | null): LockSlot | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<LockSlot>;
    if (
      typeof value.choosing !== 'boolean'
      || !Number.isSafeInteger(value.ticket)
      || (value.ticket ?? 0) < 0
      || !Number.isFinite(value.expiresAt)
    ) {
      return null;
    }
    return value as LockSlot;
  } catch {
    return null;
  }
}

function listActiveSlots(storage: Storage): Array<{ id: string; slot: LockSlot }> {
  const now = Date.now();
  const slots: Array<{ id: string; slot: LockSlot }> = [];
  for (let index = storage.length - 1; index >= 0; index -= 1) {
    const key = storage.key(index);
    if (!key?.startsWith(SLOT_PREFIX) || key.endsWith('probe')) continue;
    const slot = parseSlot(storage.getItem(key));
    if (!slot || slot.expiresAt <= now) {
      storage.removeItem(key);
      continue;
    }
    slots.push({ id: key.slice(SLOT_PREFIX.length), slot });
  }
  return slots;
}

function waitForStorageChange(): Promise<void> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      window.removeEventListener('storage', onStorage);
      window.clearTimeout(timeout);
      resolve();
    };
    const onStorage = (event: StorageEvent) => {
      if (!event.key || event.key.startsWith(SLOT_PREFIX)) finish();
    };
    const timeout = window.setTimeout(finish, WAIT_TIMEOUT_MS);
    window.addEventListener('storage', onStorage);
  });
}

async function acquireStorageLock(): Promise<StorageLockHandle> {
  const storage = requireBrowserSessionStorage();
  const id = getTabId();
  const ownKey = `${SLOT_PREFIX}${id}`;
  const writeSlot = (slot: LockSlot) => storage.setItem(ownKey, JSON.stringify(slot));
  let heartbeat: number | undefined;

  try {
    writeSlot({ choosing: true, ticket: 0, expiresAt: Date.now() + LEASE_MS });
    const ticket = Math.max(
      0,
      ...listActiveSlots(storage).map(({ slot }) => slot.ticket)
    ) + 1;
    writeSlot({ choosing: false, ticket, expiresAt: Date.now() + LEASE_MS });
    // Keep waiting contenders visible too. A lease that expires before lock
    // acquisition could otherwise let a third tab enter alongside this one.
    heartbeat = window.setInterval(() => {
      try {
        const current = parseSlot(storage.getItem(ownKey));
        if (current?.ticket === ticket && current.expiresAt > Date.now()) {
          writeSlot({ ...current, expiresAt: Date.now() + LEASE_MS });
        }
      } catch {
        // The synchronous ownership guard below will fail closed.
      }
    }, HEARTBEAT_MS);

    const assertOwned = () => {
      const current = parseSlot(storage.getItem(ownKey));
      if (
        current?.ticket !== ticket
        || current.choosing
        || current.expiresAt <= Date.now()
      ) {
        throw new Error('Browser session lock lease was lost');
      }
    };

    while (true) {
      assertOwned();
      const blocker = listActiveSlots(storage).some(({ id: otherId, slot }) => (
        otherId !== id
        && (
          slot.choosing
          || slot.ticket < ticket
          || (slot.ticket === ticket && otherId < id)
        )
      ));
      if (!blocker) break;
      await waitForStorageChange();
    }

    assertOwned();

    let released = false;
    return {
      assertOwned,
      release: () => {
        if (released) return;
        released = true;
        window.clearInterval(heartbeat);
        const current = parseSlot(storage.getItem(ownKey));
        if (current?.ticket === ticket) storage.removeItem(ownKey);
      },
    };
  } catch (error) {
    if (heartbeat !== undefined) window.clearInterval(heartbeat);
    storage.removeItem(ownKey);
    throw error;
  }
}

async function withLocalQueue<T>(operation: () => Promise<T>): Promise<T> {
  const previous = localQueue;
  let releaseQueue!: () => void;
  localQueue = new Promise<void>((resolve) => {
    releaseQueue = resolve;
  });
  await previous;
  try {
    return await operation();
  } finally {
    releaseQueue();
  }
}

export async function withBrowserSessionLock<T>(
  operation: (guard: BrowserSessionLockGuard) => Promise<T>
): Promise<T> {
  return withLocalQueue(async () => {
    // A monotonic shared version is part of the security boundary even when
    // Web Locks is available. Do not mutate the shared cookie without it.
    requireBrowserSessionStorage();
    const lockManager = typeof navigator !== 'undefined' ? navigator.locks : undefined;
    if (lockManager) {
      return lockManager.request(LOCK_NAME, () => operation({ assertOwned: () => undefined }));
    }

    const storageLock = await acquireStorageLock();
    try {
      storageLock.assertOwned();
      return await operation(storageLock);
    } finally {
      storageLock.release();
    }
  });
}

function parseVersion(raw: string | null): BrowserSessionVersion | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as Partial<BrowserSessionVersion>;
    if (
      !Number.isSafeInteger(value.revision)
      || (value.revision ?? -1) < 0
      || !Number.isSafeInteger(value.sequence)
      || (value.sequence ?? -1) < 0
    ) {
      return null;
    }
    let state = value.state === 'active' || value.state === 'cleared'
      ? value.state
      : 'unknown';
    let sessionId: string | null = null;
    if (state === 'active') {
      if (value.sessionId === undefined || value.sessionId === null) {
        // Legacy records are reconciled by the next browser refresh.
        sessionId = null;
      } else if (
        typeof value.sessionId === 'string'
        && /^[A-Za-z0-9_-]{16,128}$/.test(value.sessionId)
      ) {
        sessionId = value.sessionId;
      } else {
        state = 'unknown';
      }
    }
    return {
      revision: value.revision,
      sequence: value.sequence,
      state,
      sessionId,
    } as BrowserSessionVersion;
  } catch {
    return null;
  }
}

export function readBrowserSessionVersion(): BrowserSessionVersion {
  if (typeof window === 'undefined') {
    return { revision: 0, sequence: 0, state: 'unknown', sessionId: null };
  }
  try {
    return parseVersion(window.localStorage.getItem(VERSION_KEY)) ?? {
      revision: 0,
      sequence: 0,
      state: 'unknown',
      sessionId: null,
    };
  } catch {
    return { revision: 0, sequence: 0, state: 'unknown', sessionId: null };
  }
}

export function requireBrowserSessionVersion(): BrowserSessionVersion {
  const storage = requireBrowserSessionStorage();
  return parseVersion(storage.getItem(VERSION_KEY)) ?? {
    revision: 0,
    sequence: 0,
    state: 'unknown',
    sessionId: null,
  };
}

export function advanceBrowserSessionVersion(
  replaceSession: boolean,
  state: 'active' | 'cleared',
  sessionId: string | null,
  minimum: BrowserSessionVersion = {
    revision: 0,
    sequence: 0,
    state: 'unknown',
    sessionId: null,
  }
): BrowserSessionVersion {
  if (
    (state === 'active' && (
      typeof sessionId !== 'string'
      || !/^[A-Za-z0-9_-]{16,128}$/.test(sessionId)
    ))
    || (state === 'cleared' && sessionId !== null)
  ) {
    throw new Error('Browser session identifier is invalid');
  }
  const storage = requireBrowserSessionStorage();
  const stored = parseVersion(storage.getItem(VERSION_KEY)) ?? {
    revision: 0,
    sequence: 0,
    state: 'unknown' as const,
    sessionId: null,
  };
  const current = (
    stored.revision > minimum.revision
    || (
      stored.revision === minimum.revision
      && stored.sequence >= minimum.sequence
    )
  ) ? stored : minimum;
  const next = {
    revision: current.revision + (replaceSession ? 1 : 0),
    sequence: current.sequence + 1,
    state,
    sessionId,
  };
  if (!Number.isSafeInteger(next.revision) || !Number.isSafeInteger(next.sequence)) {
    throw new Error('Browser session version overflow');
  }
  storage.setItem(VERSION_KEY, JSON.stringify(next));
  const persisted = parseVersion(storage.getItem(VERSION_KEY));
  if (
    !persisted
    || persisted.revision !== next.revision
    || persisted.sequence !== next.sequence
    || persisted.state !== next.state
    || persisted.sessionId !== next.sessionId
  ) {
    throw new Error('Browser session version could not be persisted');
  }
  return next;
}

export function subscribeToBrowserSessionVersion(
  listener: (version: BrowserSessionVersion) => void
): () => void {
  if (typeof window === 'undefined') return () => undefined;
  const onStorage = (event: StorageEvent) => {
    if (event.key !== VERSION_KEY) return;
    const version = parseVersion(event.newValue);
    if (version) listener(version);
  };
  window.addEventListener('storage', onStorage);
  return () => window.removeEventListener('storage', onStorage);
}
