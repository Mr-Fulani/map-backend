import {
  AxiosError,
  AxiosHeaders,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios';


export class MemoryStorage implements Storage {
  private readonly values = new Map<string, string>();

  get length() {
    return this.values.size;
  }

  clear() {
    this.values.clear();
  }

  getItem(key: string) {
    return this.values.get(key) ?? null;
  }

  key(index: number) {
    return Array.from(this.values.keys())[index] ?? null;
  }

  removeItem(key: string) {
    this.values.delete(key);
  }

  setItem(key: string, value: string) {
    this.values.set(String(key), String(value));
  }
}


export function response(
  config: InternalAxiosRequestConfig,
  data: unknown,
  status = 200,
): AxiosResponse {
  return {
    config,
    data,
    headers: new AxiosHeaders(),
    status,
    statusText: String(status),
  };
}


export function failure(
  config: InternalAxiosRequestConfig,
  status: number,
  data: unknown,
) {
  const failedResponse = response(config, data, status);
  return new AxiosError(
    `HTTP ${status}`,
    'ERR_BAD_RESPONSE',
    config,
    undefined,
    failedResponse,
  );
}


export function requestBody(config: InternalAxiosRequestConfig) {
  return typeof config.data === 'string' ? JSON.parse(config.data) : config.data;
}


export function authorization(config: InternalAxiosRequestConfig) {
  return config.headers.get('Authorization')?.toString() ?? '';
}

export function installBrowserEnvironment(pathname = '/dashboard') {
  const events = new EventTarget();
  const localStorage = new MemoryStorage();
  const sessionStorage = new MemoryStorage();
  let replacedLocation: string | null = null;
  const browserWindow = {
    localStorage,
    sessionStorage,
    location: {
      origin: 'https://app.example.test',
      pathname,
      search: '',
      hash: '',
      replace: (href: string) => {
        replacedLocation = href;
      },
    },
    addEventListener: events.addEventListener.bind(events),
    removeEventListener: events.removeEventListener.bind(events),
    dispatchEvent: events.dispatchEvent.bind(events),
    setTimeout: globalThis.setTimeout.bind(globalThis),
    clearTimeout: globalThis.clearTimeout.bind(globalThis),
    setInterval: globalThis.setInterval.bind(globalThis),
    clearInterval: globalThis.clearInterval.bind(globalThis),
  };

  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: browserWindow,
    writable: true,
  });
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: localStorage,
    writable: true,
  });
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: sessionStorage,
    writable: true,
  });
  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: {},
    writable: true,
  });
  Object.defineProperty(globalThis, 'BroadcastChannel', {
    configurable: true,
    value: undefined,
    writable: true,
  });

  return {
    dispatchStorageEvent: (key: string, newValue: string | null) => {
      const event = new Event('storage');
      Object.defineProperties(event, {
        key: { value: key },
        newValue: { value: newValue },
      });
      events.dispatchEvent(event);
    },
    localStorage,
    sessionStorage,
    replacedLocation: () => replacedLocation,
  };
}
