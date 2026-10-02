// Types and helpers for the GigGuard API. Money is always integer HKD cents.

export type Entry = {
  seq: number;
  timestamp: string;
  prev_hash: string;
  policy_hash: string;
  event_type: string;
  inputs: Record<string, any>;
  rule_id: string | null;
  decision: "allow" | "deny" | "escalate" | null;
  reason: string | null;
  entry_hash: string;
  signature: string;
};

export type Scenario = {
  id: string;
  title: string;
  description: string;
  mandate: Record<string, any>;
  freelancer_count: number;
};

export type Health = {
  llm_mode: string;
  agent_model: string;
  fast_model: string;
  key_mode: string;
  agent_pubkey: string;
};

export type RunStatus = {
  run_id: string;
  done: boolean;
  error: string | null;
  status?: "hired" | "escalated" | "no_hire";
  hired?: string | null;
  spent_minor?: number;
  expectations_passed?: boolean;
};

export type VerifyResult = {
  ok: boolean;
  message: string;
  checked: number;
  broken_seq: number | null;
  head: string;
  pubkey: string;
  tampered_seq: number | null;
};

export async function getJSON<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) throw new Error(`${r.status} ${url}`);
  return r.json();
}

/** 123456 -> "HKD 1,234.56" using integer math only. */
export function hkd(minor: number): string {
  const sign = minor < 0 ? "-" : "";
  const abs = Math.abs(minor);
  const whole = Math.floor(abs / 100).toLocaleString("en-US");
  return `${sign}HKD ${whole}.${String(abs % 100).padStart(2, "0")}`;
}
