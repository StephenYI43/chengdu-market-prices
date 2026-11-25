import { useEffect, useRef, useState } from 'react';
import { readJson } from './api';

export interface ImportPreview {
  preview_id: string; filename: string; file_sha256: string; file_bytes: number;
  source_url: string; sheet: string; base_run_id: string; source_rows: number;
  date_start: string | null; date_end: string | null; can_confirm: boolean;
  counts: Record<'new' | 'existing_duplicate' | 'file_duplicate' | 'conflict' | 'invalid', number>;
  sample: PreviewRow[]; error_examples: PreviewRow[];
}
export interface PreviewRow {
  input_row: number; sheet: string; classification: string; errors: string[];
  quote_date: string; product_name: string; product_spec: string; unit: string;
  district: string; market: string; price_raw: string;
}
export interface ImportJob {
  id: string; filename: string; status: string; source_rows: number; expected_rows: number;
  published_rows: number; received_rows: number; inserted_rows: number; skipped_rows: number;
  created_at: string; updated_at: string; finished_at: string | null;
  result_run_id: string | null; error_message: string | null; message: string;
  recovery_count: number;
}
export interface ImportService {
  worker: { online: boolean; phase: string; last_heartbeat: string | null };
  current_run_id: string | null; last_event_id: number;
}

export function useImportEvents(activeRunId?: string) {
  const [jobs, setJobs] = useState<ImportJob[]>([]);
  const [service, setService] = useState<ImportService>();
  const [connected, setConnected] = useState(false);
  const [newRunId, setNewRunId] = useState<string | null>(null);
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  function acceptJob(job: ImportJob) {
    setJobs(previous => [job, ...previous.filter(item => item.id !== job.id)]
      .sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 20));
  }
  useEffect(() => {
    if (!activeRunId) return;
    const controller = new AbortController();
    let events: EventSource | undefined;
    setNewRunId(null);
    async function refresh() {
      const [state, list] = await Promise.all([
        readJson<ImportService>('/api/v1/imports/service', controller.signal),
        readJson<{ items: ImportJob[] }>('/api/v1/jobs', controller.signal),
      ]);
      if (controller.signal.aborted) return;
      setService(state); setJobs(list.items);
      setNewRunId(state.current_run_id !== activeRunId ? state.current_run_id : null);
    }
    refreshRef.current = refresh;
    async function start() {
      await refresh();
      if (controller.signal.aborted) return;
      const state = await readJson<ImportService>('/api/v1/imports/service', controller.signal);
      if (controller.signal.aborted) return;
      setService(state);
      setNewRunId(state.current_run_id !== activeRunId ? state.current_run_id : null);
      events = new EventSource(`/api/v1/stream/events?after=${state.last_event_id}`);
      events.onopen = () => setConnected(true);
      events.onerror = () => setConnected(false);
      events.addEventListener('job.updated', event => acceptJob(JSON.parse((event as MessageEvent).data)));
      events.addEventListener('snapshot.published', event => {
        const value = JSON.parse((event as MessageEvent).data) as { run_id: string };
        if (value.run_id !== activeRunId) setNewRunId(value.run_id);
      });
    }
    void start().catch(() => { if (!controller.signal.aborted) setConnected(false); });
    const timer = window.setInterval(() => { void refresh().catch(() => {}); }, 5000);
    return () => { controller.abort(); events?.close(); window.clearInterval(timer); setConnected(false); };
  }, [activeRunId]);
  return { jobs, service, connected, newRunId, acceptJob,
    refreshJobs: () => refreshRef.current().catch(() => {}) };
}

export type ImportLive = ReturnType<typeof useImportEvents>;
