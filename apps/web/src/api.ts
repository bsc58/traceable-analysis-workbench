export class ApiError extends Error {
  constructor(public code: string, message: string, public status: number) {super(message);}
}
export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch('/api' + path, {...options, headers:{'Content-Type':'application/json',...options.headers}});
  const data = await response.json();
  if (!response.ok) throw new ApiError(data.error?.code ?? `HTTP_${response.status}`,
    data.error?.message ?? JSON.stringify(data.detail ?? data), response.status);
  return data;
}
export function submissionAttempt(previous: {body:string; key:string} | null, body:string) {
  return previous?.body === body ? previous : {body, key:crypto.randomUUID()};
}
