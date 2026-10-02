import { useEffect, useRef, useState } from "react";
import { Entry, Health, RunStatus, Scenario, VerifyResult, getJSON, hkd } from "./api";

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [runId, setRunId] = useState<string | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [status, setStatus] = useState<RunStatus | null>(null);
  const [verify, setVerify] = useState<VerifyResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const source = useRef<EventSource | null>(null);

  useEffect(() => {
    getJSON<Health>("/api/health").then(setHealth).catch(() => setError("API not reachable. Run `make run`."));
    getJSON<Scenario[]>("/api/scenarios").then((s) => {
      setScenarios(s);
      setSelected(s[0]?.id ?? "");
    }).catch(() => {});
    return () => source.current?.close();
  }, []);

  async function start() {
    source.current?.close();
    setEntries([]);
    setStatus(null);
    setVerify(null);
    const { run_id } = await getJSON<{ run_id: string }>("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scenario_id: selected }),
    });
    setRunId(run_id);
    const es = new EventSource(`/api/runs/${run_id}/events`);
    es.addEventListener("entry", (e) => setEntries((prev) => [...prev, JSON.parse((e as MessageEvent).data)]));
    es.addEventListener("done", (e) => {
      setStatus(JSON.parse((e as MessageEvent).data));
      es.close();
    });
    source.current = es;
  }

  async function runVerify(tamper: boolean) {
    if (!runId) return;
    setVerify(await getJSON<VerifyResult>(`/api/runs/${runId}/verify${tamper ? "?tamper=true" : ""}`));
  }

  const running = runId !== null && status === null;
  const mandate = entries.find((e) => e.event_type === "run_started")?.inputs.signed_mandate?.mandate;
  const head = entries.length ? entries[entries.length - 1].entry_hash : null;

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-4 py-3">
          <h1 className="text-lg font-semibold">GigGuard</h1>
          <span className="rounded bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900">
            SIMULATED: no real marketplaces or payments
          </span>
          {health && (
            <span className="ml-auto text-xs text-slate-500">
              LLM: <b>{health.llm_mode.toUpperCase()}</b> · agent {health.agent_model} · personas {health.fast_model} ·
              keys {health.key_mode.toUpperCase()}
            </span>
          )}
        </div>
      </header>

      {error && <div className="mx-auto max-w-7xl px-4 pt-4 text-sm text-red-700">{error}</div>}

      <main className="mx-auto grid max-w-7xl gap-4 px-4 py-4 lg:grid-cols-[260px_1fr_320px]">
        {/* Scenarios */}
        <section className="space-y-2">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Scenarios</h2>
          {scenarios.map((s) => (
            <button
              key={s.id}
              onClick={() => setSelected(s.id)}
              className={`block w-full rounded-lg border p-3 text-left text-sm ${
                selected === s.id ? "border-indigo-500 bg-indigo-50" : "border-slate-200 bg-white hover:border-slate-300"
              }`}
            >
              <div className="font-medium">{s.title}</div>
              <div className="mt-1 text-xs text-slate-600">{s.description}</div>
            </button>
          ))}
          <button
            onClick={start}
            disabled={!selected || running}
            className="w-full rounded-lg bg-indigo-600 px-3 py-2 text-sm font-semibold text-white disabled:opacity-50"
          >
            {running ? "Running…" : "Run agent"}
          </button>
        </section>

        {/* Timeline */}
        <section className="min-w-0 space-y-3">
          {mandate && <MandateCard m={mandate} />}
          {status && <Outcome s={status} />}
          <ol className="space-y-2">
            {entries.map((e) => (
              <EntryRow key={e.seq} e={e} />
            ))}
          </ol>
          {!runId && <p className="text-sm text-slate-500">Pick a scenario and run the agent. Each step appears as a signed log entry.</p>}
        </section>

        {/* Verification */}
        <section className="space-y-3">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Verify the log</h2>
          <div className="space-y-2 rounded-lg border border-slate-200 bg-white p-3 text-sm">
            <p className="text-slate-600">
              Every entry is hash-chained and Ed25519-signed. Anyone can check it offline with
              <code className="mx-1 rounded bg-slate-100 px-1">verifier/verify_log.py</code>.
            </p>
            <div className="flex gap-2">
              <button disabled={!status} onClick={() => runVerify(false)}
                className="rounded border border-slate-300 px-2 py-1 disabled:opacity-50">Verify</button>
              <button disabled={!status} onClick={() => runVerify(true)}
                className="rounded border border-red-300 px-2 py-1 text-red-700 disabled:opacity-50">Tamper, then verify</button>
            </div>
            {verify && (
              <div className={`rounded p-2 text-xs ${verify.ok ? "bg-emerald-50 text-emerald-900" : "bg-red-50 text-red-900"}`}>
                {verify.tampered_seq !== null && <div>Flipped the decision in entry #{verify.tampered_seq}.</div>}
                <div className="break-all font-mono">{verify.ok ? `PASS: ${verify.message}` : `FAIL: ${verify.message}`}</div>
              </div>
            )}
            {runId && status && (
              <a className="block text-indigo-700 underline" href={`/api/runs/${runId}/log.jsonl`} download={`gigguard-${runId}.jsonl`}>
                Download log.jsonl
              </a>
            )}
            {status && head && (
              <div className="rounded border border-indigo-200 bg-indigo-50 p-2 text-xs">
                <div className="font-semibold text-indigo-900">Pinned head hash</div>
                <div className="break-all font-mono text-indigo-900">{head}</div>
                <div className="mt-1 text-indigo-800">
                  Check a copy with <code>--expect-head {head.slice(0, 8)}…</code> to catch entries cut off the end.
                </div>
              </div>
            )}
            {health && (
              <div className="text-xs text-slate-500">
                Agent public key
                <div className="break-all font-mono text-slate-700">{health.agent_pubkey}</div>
              </div>
            )}
          </div>
        </section>
      </main>
    </div>
  );
}

function MandateCard({ m }: { m: Record<string, any> }) {
  const p = m.policy;
  const rows: [string, string][] = [
    ["Task", m.task],
    ["Budget cap", hkd(p.budget_cap.amount_minor)],
    ["Auto-approve up to", p.auto_approve_limit ? hkd(p.auto_approve_limit.amount_minor) : "no limit"],
    ["Eligibility", `≥ ${(p.eligibility.min_rating_x100 / 100).toFixed(2)}★, ≥ ${p.eligibility.min_completed_orders} orders${p.eligibility.verified_required ? ", verified" : ""}`],
    ["Velocity", `${p.velocity.max_orders} orders / ${p.velocity.window_minutes} min`],
    ["Payment", `${p.milestone_only ? "milestones only" : "any split"}${p.forbid_offplatform ? ", on-platform only" : ""}`],
    ["Escalations expire after", `${Math.round(p.escalation_ttl_seconds / 3600)} h`],
    ["Expires", p.expires_at],
  ];
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <div className="mb-2 flex items-center gap-2 text-sm font-semibold">
        Signed mandate <span className="font-mono text-xs font-normal text-slate-500">{p.policy_id} v{p.version}</span>
      </div>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-4">
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt className="text-slate-500">{k}</dt>
            <dd className="font-medium">{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function Outcome({ s }: { s: RunStatus }) {
  if (s.error) return <div className="rounded-lg bg-red-50 p-3 text-sm text-red-900">Run failed: {s.error}</div>;
  const text = {
    hired: `Hired ${s.hired} for ${hkd(s.spent_minor ?? 0)} (held in SIMULATED escrow)`,
    escalated: "Escalated: a human must approve before anything is spent",
    no_hire: `No hire. ${hkd(s.spent_minor ?? 0)} spent.`,
  }[s.status ?? "no_hire"];
  const color = { hired: "bg-emerald-50 text-emerald-900", escalated: "bg-amber-50 text-amber-900", no_hire: "bg-slate-100 text-slate-800" }[
    s.status ?? "no_hire"
  ];
  return <div className={`rounded-lg p-3 text-sm font-medium ${color}`}>{text}</div>;
}

const OUTCOME_STYLE: Record<string, string> = {
  allow: "bg-emerald-100 text-emerald-900",
  deny: "bg-red-100 text-red-900",
  escalate: "bg-amber-100 text-amber-900",
};

function EntryRow({ e }: { e: Entry }) {
  const p = e.inputs;
  let body: React.ReactNode = <span className="text-slate-500">{JSON.stringify(p).slice(0, 160)}</span>;

  switch (e.event_type) {
    case "gate_decision": {
      const a = p.action;
      const target =
        a?.kind === "hire" ? `hire ${a.proposal.freelancer_id}`
        : a?.kind === "send_message" ? `message ${a.message.freelancer_id}`
        : a?.kind === "release" ? `re-check before paying ${a.proposal_id}`
        : a?.kind === "verify_mandate" ? "verify mandate signature"
        : "";
      body = (
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={`rounded px-1.5 py-0.5 text-xs font-bold uppercase ${OUTCOME_STYLE[e.decision ?? "deny"]}`}>{e.decision}</span>
            <span className="font-mono text-xs">{e.rule_id}</span>
            <span className="text-xs text-slate-500">{target}</span>
          </div>
          <div className="font-mono text-xs">{e.reason}</div>
          {a?.kind === "send_message" && <div className="text-xs text-slate-500">“{a.message.text}”</div>}
          {a?.kind === "hire" && a.proposal.milestones.length > 0 && (
            <div className="text-xs text-slate-500">
              Milestones: {a.proposal.milestones.map((m: any) => hkd(m.amount.amount_minor)).join(" + ")}
            </div>
          )}
        </div>
      );
      break;
    }
    case "message_received":
      body = (
        <div>
          <span className="mr-2 rounded bg-slate-200 px-1.5 py-0.5 text-xs font-semibold">UNTRUSTED</span>
          <span className="text-xs text-slate-500">{p.message.freelancer_id} wrote:</span>
          <div className="mt-1 whitespace-pre-wrap rounded bg-slate-50 p-2 text-xs">{p.message.text}</div>
        </div>
      );
      break;
    case "quote_parsed":
      body = (
        <span>
          {p.freelancer_id}: price read as <b>{p.status}</b>
          {p.amount_minor !== null && ` (${hkd(p.amount_minor)})`}
          {p.status === "ambiguous" && ` from ${p.found.map(hkd).join(", ") || "mixed currencies"}; ignored`}
        </span>
      );
      break;
    case "scorecard": {
      const c = p.scorecard;
      body = c.eligible ? (
        <span>{c.freelancer_id}: eligible, fit {c.fit_score}/100</span>
      ) : (
        <span>{c.freelancer_id}: <b>not eligible</b> ({c.eligibility_failures.join(", ")}), not contacted</span>
      );
      break;
    }
    case "candidate_excluded":
      body = <span>{p.freelancer_id} excluded ({p.code})</span>;
      break;
    case "shortlist":
      body = <span>Shortlist: {p.freelancer_ids.join(", ") || "nobody"}</span>;
      break;
    case "search_results":
      body = <span>Found {p.freelancer_ids.length} SIMULATED freelancers: {p.freelancer_ids.join(", ")}</span>;
      break;
    case "offers_ranked":
      body = <span>Offers by total: {p.scorecards.map((c: any) => `#${c.rank} ${c.freelancer_id} ${hkd(c.total_cost.amount_minor)}`).join(" · ") || "none"}</span>;
      break;
    case "order_created":
      body = (
        <span>
          {p.order.order_id} for {p.order.freelancer_id}: {hkd(p.order.total.amount_minor)} held in SIMULATED escrow
          {p.order.milestones.length > 0 && ` across ${p.order.milestones.length} milestones`}
        </span>
      );
      break;
    case "hire_approved":
      body = <span>Approved {p.proposal_id} for {hkd(p.total_minor)}. Confirming with the freelancer before any money moves.</span>;
      break;
    case "hire_cancelled":
      body = <span><b>Cancelled</b> {p.proposal_id} before payment ({p.rule_id}). No money moved.</span>;
      break;
    case "policy_revoked":
      body = <span><b>User revoked the mandate</b> {p.policy_id}. Everything from now on is denied.</span>;
      break;
    case "escalation_requested":
      body = <span>Waiting for a human to approve {p.freelancer_id} ({p.rule_id}). Counts as denied after {p.expires_at}.</span>;
      break;
    case "draft_ready":
      body = <span>Intro for {p.freelancer_id} drafted by {p.source}{p.code ? ` (${p.code})` : ""}</span>;
      break;
    case "run_started":
      body = <span>Run started · LLM {p.llm_mode} · mandate digest <span className="font-mono">{p.mandate_digest.slice(0, 12)}</span></span>;
      break;
    case "run_finished":
      body = <span>Finished: {p.status}{p.hired ? ` (${p.hired})` : ""}, spent {hkd(p.spent_minor)}</span>;
      break;
  }

  return (
    <li className="rounded-lg border border-slate-200 bg-white p-2 text-sm">
      <div className="mb-1 flex items-center gap-2 text-xs text-slate-400">
        <span className="font-mono">#{e.seq}</span>
        <span>{e.event_type}</span>
        <span className="ml-auto font-mono" title={`entry_hash ${e.entry_hash}\nprev_hash ${e.prev_hash}\npolicy_hash ${e.policy_hash}`}>
          {e.entry_hash.slice(0, 10)}…
        </span>
      </div>
      {body}
    </li>
  );
}
