import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { copy } from "./i18n";
import "./style.css";
type Lang = "en" | "el";
type Settings = {
  company: string;
  language: Lang;
  latest_year: number;
  currency: string;
  scope: string;
  money_scale: number;
  share_scale: number;
};
type Metric = {
  id: string;
  label: Record<Lang, string>;
  sheet: Record<Lang, string>;
  unit: string;
  automatic: boolean;
};
type Candidate = {
  id: string;
  metric_id: string;
  year: number;
  value: string;
  file: string;
  page: number;
  quote: string;
  context_quote: string;
  document_id: string;
  status: string;
  raw_value: string;
  scale: number;
};
type Decision = {
  metric_id: string;
  year: number;
  candidate_id?: string;
  manual_value?: string;
  note?: string;
};
type SavedDecision = Candidate & { manual: boolean; note: string };
type Job = {
  id: string;
  status: string;
  settings: Settings;
  files: { name: string }[];
  candidates: Candidate[];
  decisions: SavedDecision[];
  checks: {
    year: number;
    check: string;
    status: string;
    difference: string | null;
  }[];
  warnings: string[];
  rejected: { file: string; page: number; reason: string }[];
  progress: { done: number; total: number };
  error: string | null;
};
function App() {
  const [lang, setLang] = useState<Lang>("en"),
    t = copy[lang];
  const [settings, setSettings] = useState<Settings>({
    company: "",
    language: "en",
    latest_year: new Date().getFullYear() - 1,
    currency: "EUR",
    scope: "consolidated",
    money_scale: 1000000,
    share_scale: 1000000,
  });
  const [files, setFiles] = useState<File[]>([]),
    [metrics, setMetrics] = useState<Metric[]>([]),
    [history, setHistory] = useState<
      { id: string; status: string; settings: Settings }[]
    >([]);
  const [job, setJob] = useState<Job | null>(null),
    [draft, setDraft] = useState<Record<string, Decision>>({}),
    [dirty, setDirty] = useState(false);
  const [error, setError] = useState(""),
    [message, setMessage] = useState(""),
    [busy, setBusy] = useState(false),
    [key, setKey] = useState("");
  const [year, setYear] = useState(settings.latest_year),
    [filter, setFilter] = useState(""),
    [query, setQuery] = useState(""),
    [view, setView] = useState("all");
  const [answer, setAnswer] = useState<{
    answer: string;
    sources: {
      citation: number;
      file: string;
      page: number;
      document_id: string;
    }[];
  } | null>(null);
  const [editing, setEditing] = useState<string | null>(null),
    [manual, setManual] = useState(""),
    [note, setNote] = useState("");
  async function api(path: string, init: RequestInit = {}) {
    const r = await fetch(`/api${path}`, {
      ...init,
      headers: {
        ...(init.body instanceof FormData
          ? {}
          : { "Content-Type": "application/json" }),
        "X-API-Key": key,
        ...init.headers,
      },
    });
    if (!r.ok) {
      const data = await r.json().catch(() => ({ detail: r.statusText }));
      throw new Error(
        typeof data.detail === "string"
          ? data.detail
          : JSON.stringify(data.detail),
      );
    }
    return r;
  }
  async function refresh() {
    try {
      const [a, b] = await Promise.all([api("/catalog"), api("/jobs")]);
      setMetrics(await a.json());
      setHistory(await b.json());
    } catch (e) {
      setError(String(e));
    }
  }
  useEffect(() => {
    void refresh();
  }, []);
  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);
  function load(j: Job) {
    setJob(j);
    setYear(j.settings.latest_year);
    setDirty(false);
    setAnswer(null);
    setMessage("");
    setEditing(null);
    setDraft(
      Object.fromEntries(
        j.decisions.map((d) => [
          `${d.metric_id}:${d.year}`,
          d.manual
            ? {
                metric_id: d.metric_id,
                year: d.year,
                manual_value: d.value,
                note: d.note,
              }
            : { metric_id: d.metric_id, year: d.year, candidate_id: d.id },
        ]),
      ),
    );
  }
  useEffect(() => {
    if (!job || !["queued", "extracting"].includes(job.status)) return;
    let cancelled = false;
    const id = setInterval(async () => {
      try {
        const j = await (await api(`/jobs/${job.id}`)).json();
        if (!cancelled) {
          setJob(j);
          if (!["queued", "extracting"].includes(j.status)) void refresh();
        }
      } catch (e) {
        if (!cancelled) setError(String(e));
      }
    }, 2000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [job?.id, job?.status, key]);
  async function action(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await fn();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  async function start() {
    if (!settings.company.trim() || !files.length) {
      setError(t.required);
      return;
    }
    await action(async () => {
      const f = new FormData();
      f.append("settings", JSON.stringify({ ...settings, language: lang }));
      files.forEach((x) => f.append("files", x));
      load(await (await api("/jobs", { method: "POST", body: f })).json());
      await refresh();
    });
  }
  async function download(kind: "workbook" | "audit") {
    if (!job) return;
    await action(async () => {
      const r = await api(
        `/jobs/${job.id}/${kind}${kind === "workbook" ? `?language=${lang}` : ""}`,
      );
      const url = URL.createObjectURL(await r.blob());
      const a = document.createElement("a");
      a.href = url;
      a.download =
        kind === "workbook"
          ? `financial-analysis-${lang}-${job.settings.latest_year}.xlsx`
          : "financial-evidence.json";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    });
  }
  async function openDoc(id: string, page: number) {
    if (!job) return;
    await action(async () => {
      const r = await api(`/jobs/${job.id}/documents/${id}`);
      const url = URL.createObjectURL(await r.blob());
      const a = document.createElement("a");
      a.href = `${url}#page=${page}`;
      a.target = "_blank";
      a.rel = "noopener";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    });
  }
  const grouped = new Map<string, Candidate[]>();
  job?.candidates.forEach((c) => {
    const k = `${c.metric_id}:${c.year}`;
    grouped.set(k, [...(grouped.get(k) || []), c]);
  });
  const conflicts = [...grouped.values()].filter(
    (cs) => new Set(cs.map((c) => c.value)).size > 1,
  ).length;
  const available = job && ["review", "ready"].includes(job.status);
  const status = (s: string) =>
    ({
      queued: t.queued,
      extracting: t.extracting,
      ready: t.ready,
      failed: t.failed,
      review: t.reviewStatus,
    })[s] || s;
  function select(k: string, d?: Decision) {
    setDraft((old) => {
      const next = { ...old };
      if (d) next[k] = d;
      else delete next[k];
      return next;
    });
    setDirty(true);
    setMessage("");
  }
  const filtered = metrics.filter((m) => {
    const k = `${m.id}:${year}`,
      cs = grouped.get(k) || [];
    return (
      `${m.label[lang]} ${m.sheet[lang]}`
        .toLowerCase()
        .includes(filter.toLowerCase()) &&
      (view === "all" ||
        (view === "missing" && !draft[k]) ||
        (view === "conflicts" && new Set(cs.map((c) => c.value)).size > 1))
    );
  });
  return (
    <>
      <header>
        <a className="brand" href="#">
          <span className="brand-icon">F</span>
          {t.brand}
        </a>
        <div className="languages" aria-label={t.language}>
          <button
            className={lang === "en" ? "active" : ""}
            onClick={() => setLang("en")}
          >
            English
          </button>
          <button
            className={lang === "el" ? "active" : ""}
            onClick={() => setLang("el")}
          >
            Ελληνικά
          </button>
        </div>
      </header>
      <main>
        <section className="hero">
          <span className="eyebrow">{t.tag}</span>
          <h1>{t.title}</h1>
          <p>{t.intro}</p>
        </section>
        <div className="workspace">
          <aside>
            <section className="panel">
              <h2>{t.new}</h2>
              <label>
                {t.company}
                <input
                  value={settings.company}
                  onChange={(e) =>
                    setSettings({ ...settings, company: e.target.value })
                  }
                  placeholder="Motor Oil (Hellas)"
                  maxLength={200}
                />
              </label>
              <div className="pair">
                <label>
                  {t.year}
                  <input
                    type="number"
                    min="1900"
                    max="2100"
                    value={settings.latest_year}
                    onChange={(e) =>
                      setSettings({
                        ...settings,
                        latest_year: Number(e.target.value),
                      })
                    }
                  />
                </label>
                <label>
                  {t.currency}
                  <input
                    value={settings.currency}
                    maxLength={3}
                    onChange={(e) =>
                      setSettings({
                        ...settings,
                        currency: e.target.value.toUpperCase(),
                      })
                    }
                  />
                </label>
              </div>
              <label>
                {t.scope}
                <select
                  value={settings.scope}
                  onChange={(e) =>
                    setSettings({ ...settings, scope: e.target.value })
                  }
                >
                  <option value="consolidated">{t.consolidated}</option>
                  <option value="standalone">{t.standalone}</option>
                </select>
              </label>
              <div className="pair">
                {(["money_scale", "share_scale"] as const).map((field) => (
                  <label key={field}>
                    {field === "money_scale" ? t.money : t.shares}
                    <select
                      value={settings[field]}
                      onChange={(e) =>
                        setSettings({
                          ...settings,
                          [field]: Number(e.target.value),
                        })
                      }
                    >
                      {[1, 1000, 1000000].map((n) => (
                        <option key={n} value={n}>
                          {n.toLocaleString(lang === "el" ? "el-GR" : "en-GB")}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
              <label className="upload">
                <span className="upload-icon">↑</span>
                <strong>{t.upload}</strong>
                <input
                  type="file"
                  accept="application/pdf,.pdf"
                  multiple
                  onChange={(e) => setFiles(Array.from(e.target.files || []))}
                />
                <small>{t.uploadHelp}</small>
              </label>
              {files.length > 0 && (
                <ul className="file-list">
                  {files.map((f, i) => (
                    <li key={i}>
                      {f.name}
                      <small>{(f.size / 1024 / 1024).toFixed(1)} MB</small>
                    </li>
                  ))}
                </ul>
              )}
              <button
                className="primary full"
                disabled={
                  busy ||
                  (!!job && ["queued", "extracting"].includes(job.status))
                }
                onClick={start}
              >
                {busy ? t.loading : t.start}
              </button>
              <small className="disclosure">{t.provider}</small>
            </section>
            <section className="panel history">
              <h3>{t.history}</h3>
              {history.length === 0 ? (
                <p className="muted">{t.noHistory}</p>
              ) : (
                history.map((j) => (
                  <button
                    key={j.id}
                    className={job?.id === j.id ? "current" : ""}
                    disabled={busy}
                    onClick={() =>
                      action(async () =>
                        load(await (await api(`/jobs/${j.id}`)).json()),
                      )
                    }
                  >
                    <strong>{j.settings.company}</strong>
                    <span>
                      {j.settings.latest_year} · {status(j.status)}
                    </span>
                  </button>
                ))
              )}
            </section>
            <details className="access">
              <summary>{t.access}</summary>
              <input
                type="password"
                autoComplete="off"
                aria-label={t.access}
                value={key}
                onChange={(e) => setKey(e.target.value)}
              />
              <button onClick={() => action(refresh)}>{t.unlock}</button>
            </details>
          </aside>
          <div className="content">
            {error && (
              <div role="alert" className="notice error">
                {error}
              </div>
            )}
            {message && (
              <div role="status" className="notice">
                {message}
              </div>
            )}
            {!job ? (
              <section className="panel empty">
                <div className="document-mark">
                  PDF <span>→</span> XLSX
                </div>
                <h2>{t.noJob}</h2>
                <p>{t.noJobHelp}</p>
                <div className="features">
                  <div>
                    <b>01</b>
                    {t.statements}
                  </div>
                  <div>
                    <b>02</b>
                    {t.kpis}
                  </div>
                  <div>
                    <b>03</b>
                    {t.evidence}
                  </div>
                </div>
              </section>
            ) : (
              <>
                <section className="panel job-heading">
                  <div>
                    <span className="eyebrow">{status(job.status)}</span>
                    <h2>{job.settings.company}</h2>
                    <p>
                      {job.settings.latest_year - 5}–{job.settings.latest_year}{" "}
                      · {job.settings.currency} ·{" "}
                      {job.settings.scope === "consolidated"
                        ? t.consolidated
                        : t.standalone}
                    </p>
                    <small>
                      {t.money}: {job.settings.money_scale.toLocaleString()} ·{" "}
                      {t.shares}: {job.settings.share_scale.toLocaleString()}
                    </small>
                  </div>
                  {!["queued", "extracting"].includes(job.status) && (
                    <button
                      className="danger subtle"
                      disabled={busy}
                      onClick={() => {
                        if (window.confirm(t.confirmDelete))
                          void action(async () => {
                            await api(`/jobs/${job.id}`, { method: "DELETE" });
                            setJob(null);
                            setDraft({});
                            await refresh();
                          });
                      }}
                    >
                      {t.delete}
                    </button>
                  )}
                </section>
                {["queued", "extracting"].includes(job.status) && (
                  <section className="panel progress">
                    <h3>{status(job.status)}</h3>
                    <progress
                      value={job.progress.done}
                      max={job.progress.total || 1}
                    />
                    <p>
                      {job.progress.done} / {job.progress.total || "…"}
                    </p>
                    <small>{t.progressHelp}</small>
                  </section>
                )}
                {job.status === "failed" && (
                  <section className="panel">
                    <p role="alert">{job.error}</p>
                    <button
                      onClick={() =>
                        action(async () => {
                          load(
                            await (
                              await api(`/jobs/${job.id}/retry`, {
                                method: "POST",
                              })
                            ).json(),
                          );
                        })
                      }
                    >
                      {t.retry}
                    </button>
                  </section>
                )}
                {available && (
                  <>
                    <div className="stats">
                      <div>
                        <b>{Object.keys(draft).length}</b>
                        <span>{t.selected}</span>
                      </div>
                      <div>
                        <b>{metrics.length * 6 - Object.keys(draft).length}</b>
                        <span>{t.unfilled}</span>
                      </div>
                      <div>
                        <b>{conflicts}</b>
                        <span>{t.conflicts}</span>
                      </div>
                    </div>
                    <section className="panel review">
                      <div className="section-heading">
                        <h2>{t.review}</h2>
                        <p>{t.reviewHelp}</p>
                      </div>
                      <div className="toolbar">
                        <input
                          aria-label={t.filter}
                          placeholder={t.filter}
                          value={filter}
                          onChange={(e) => setFilter(e.target.value)}
                        />
                        <select
                          aria-label={t.year}
                          value={year}
                          onChange={(e) => setYear(Number(e.target.value))}
                        >
                          {Array.from(
                            { length: 6 },
                            (_, i) => job.settings.latest_year - i,
                          ).map((y) => (
                            <option key={y}>{y}</option>
                          ))}
                        </select>
                        <select
                          aria-label={t.allRows}
                          value={view}
                          onChange={(e) => setView(e.target.value)}
                        >
                          <option value="all">{t.allRows}</option>
                          <option value="missing">{t.onlyMissing}</option>
                          <option value="conflicts">{t.onlyConflicts}</option>
                        </select>
                      </div>
                      <button
                        className="subtle bulk"
                        onClick={() => {
                          const next = { ...draft };
                          grouped.forEach((cs, k) => {
                            if (
                              !next[k] &&
                              new Set(cs.map((c) => c.value)).size === 1
                            )
                              next[k] = {
                                metric_id: cs[0].metric_id,
                                year: cs[0].year,
                                candidate_id: cs[0].id,
                              };
                          });
                          setDraft(next);
                          setDirty(true);
                        }}
                      >
                        {t.all}
                      </button>
                      <div className="metric-list">
                        {filtered.length === 0 && <p>{t.noRows}</p>}
                        {filtered.map((m) => {
                          const k = `${m.id}:${year}`,
                            cs = grouped.get(k) || [],
                            d = draft[k],
                            selected = cs.find((c) => c.id === d?.candidate_id);
                          return (
                            <article className="metric" key={k}>
                              <div className="metric-title">
                                <div>
                                  <small>{m.sheet[lang]}</small>
                                  <h4>{m.label[lang]}</h4>
                                </div>
                                <span
                                  className={`badge ${d ? "selected" : cs.some((c) => c.status === "conflict") ? "conflict" : ""}`}
                                >
                                  {d
                                    ? t.accepted
                                    : cs.some((c) => c.status === "conflict")
                                      ? t.conflict
                                      : cs.length
                                        ? t.candidate
                                        : t.missing}
                                </span>
                              </div>
                              <div className="metric-actions">
                                <select
                                  aria-label={`${m.label[lang]} ${year}`}
                                  value={
                                    d?.candidate_id ||
                                    (d?.manual_value !== undefined
                                      ? "manual"
                                      : "")
                                  }
                                  onChange={(e) => {
                                    if (e.target.value === "manual") return;
                                    const c = cs.find(
                                      (c) => c.id === e.target.value,
                                    );
                                    select(
                                      k,
                                      c
                                        ? {
                                            metric_id: m.id,
                                            year,
                                            candidate_id: c.id,
                                          }
                                        : undefined,
                                    );
                                  }}
                                >
                                  <option value="">{t.blank}</option>
                                  {cs.map((c) => (
                                    <option key={c.id} value={c.id}>
                                      {Number(c.value).toLocaleString(
                                        lang === "el" ? "el-GR" : "en-GB",
                                        { maximumFractionDigits: 8 },
                                      )}{" "}
                                      — {c.file}, p.{c.page}
                                    </option>
                                  ))}
                                  {d?.manual_value !== undefined && (
                                    <option value="manual">
                                      {d.manual_value} — {t.manual}
                                    </option>
                                  )}
                                </select>
                                <button
                                  className="subtle"
                                  onClick={() => {
                                    setEditing(editing === k ? null : k);
                                    setManual(d?.manual_value || "");
                                    setNote(d?.note || "");
                                  }}
                                >
                                  {t.add}
                                </button>
                              </div>
                              {(selected || cs.length > 0) && (
                                <details>
                                  <summary>
                                    {t.source}
                                    {selected
                                      ? ` · ${selected.file} · p.${selected.page}`
                                      : ""}
                                  </summary>
                                  {(selected ? [selected] : cs).map((c) => (
                                    <div className="evidence" key={c.id}>
                                      <strong>
                                        {c.file} · p.{c.page} · {c.year}
                                      </strong>
                                      <blockquote>{c.quote}</blockquote>
                                      <p>{c.context_quote}</p>
                                      <small>
                                        {c.raw_value} ×{" "}
                                        {c.scale.toLocaleString()} → {c.value} (
                                        {m.unit})
                                      </small>
                                      <button
                                        className="subtle"
                                        onClick={() =>
                                          openDoc(c.document_id, c.page)
                                        }
                                      >
                                        {t.open}
                                      </button>
                                    </div>
                                  ))}
                                </details>
                              )}
                              {d?.manual_value !== undefined && (
                                <p className="manual-note">{d.note}</p>
                              )}
                              {editing === k && (
                                <div className="manual-editor">
                                  <p>{t.manualHelp}</p>
                                  <input
                                    type="number"
                                    step="any"
                                    aria-label={t.value}
                                    value={manual}
                                    onChange={(e) => setManual(e.target.value)}
                                  />
                                  <textarea
                                    aria-label={t.note}
                                    placeholder={t.noteHelp}
                                    value={note}
                                    onChange={(e) => setNote(e.target.value)}
                                  />
                                  <button
                                    disabled={!manual.trim() || !note.trim()}
                                    onClick={() => {
                                      select(k, {
                                        metric_id: m.id,
                                        year,
                                        manual_value: manual,
                                        note,
                                      });
                                      setEditing(null);
                                    }}
                                  >
                                    {t.add}
                                  </button>
                                </div>
                              )}
                            </article>
                          );
                        })}
                      </div>
                      <div className="export">
                        <button
                          className="primary"
                          disabled={busy}
                          onClick={() =>
                            action(async () => {
                              const j = await (
                                await api(`/jobs/${job.id}/review`, {
                                  method: "PUT",
                                  body: JSON.stringify({
                                    decisions: Object.values(draft),
                                  }),
                                })
                              ).json();
                              setJob(j);
                              setDirty(false);
                              setMessage(t.saved);
                              await refresh();
                            })
                          }
                        >
                          {t.save}
                        </button>
                        <button
                          disabled={busy || dirty || job.status !== "ready"}
                          onClick={() => download("workbook")}
                        >
                          {t.download} · {lang.toUpperCase()}
                        </button>
                        <button
                          className="subtle"
                          disabled={busy}
                          onClick={() => download("audit")}
                        >
                          {t.audit}
                        </button>
                        <small>{dirty ? t.pending : t.recalc}</small>
                      </div>
                    </section>
                    {job.checks.length > 0 && (
                      <section className="panel">
                        <h3>{t.checks}</h3>
                        <div className="check-grid">
                          {job.checks
                            .filter((c) => c.year === year)
                            .map((c) => (
                              <div key={c.check}>
                                <span>
                                  {t.checkNames[
                                    copy.en.checkNames.indexOf(c.check)
                                  ] || c.check}
                                </span>
                                <b
                                  className={
                                    c.status === "mismatch" ? "bad" : ""
                                  }
                                >
                                  {c.status === "pass"
                                    ? t.pass
                                    : c.status === "missing"
                                      ? t.missingCheck
                                      : `${t.mismatch}: ${c.difference}`}
                                </b>
                              </div>
                            ))}
                        </div>
                      </section>
                    )}
                    <section className="panel ask">
                      <h3>{t.ask}</h3>
                      <form
                        onSubmit={(e) => {
                          e.preventDefault();
                          void action(async () =>
                            setAnswer(
                              await (
                                await api(`/jobs/${job.id}/ask`, {
                                  method: "POST",
                                  body: JSON.stringify({
                                    question: query,
                                    language: lang,
                                  }),
                                })
                              ).json(),
                            ),
                          );
                        }}
                      >
                        <input
                          aria-label={t.question}
                          value={query}
                          onChange={(e) => setQuery(e.target.value)}
                          placeholder={t.question}
                          minLength={3}
                          required
                        />
                        <button disabled={busy}>
                          {busy ? t.loading : t.send}
                        </button>
                      </form>
                      {answer && (
                        <div className="answer">
                          <p>{answer.answer}</p>
                          {answer.sources.map((s) => (
                            <button
                              className="subtle"
                              key={s.citation}
                              onClick={() => openDoc(s.document_id, s.page)}
                            >
                              [{s.citation}] {s.file} · p.{s.page}
                            </button>
                          ))}
                        </div>
                      )}
                    </section>
                  </>
                )}
                {(job.warnings.length > 0 || job.rejected.length > 0) && (
                  <details className="panel">
                    <summary>
                      {t.warnings} ({job.warnings.length + job.rejected.length})
                    </summary>
                    <ul>
                      {job.warnings.map((w, i) => (
                        <li key={i}>{w}</li>
                      ))}
                      {job.rejected.map((r, i) => (
                        <li key={`r${i}`}>
                          {r.file}, p.{r.page}: {r.reason}
                        </li>
                      ))}
                    </ul>
                  </details>
                )}
              </>
            )}
          </div>
        </div>
      </main>
      <footer>
        Financial Workbench{" "}
        <span>PDF → {lang === "en" ? "Evidence" : "Τεκμηρίωση"} → Excel</span>
      </footer>
    </>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
