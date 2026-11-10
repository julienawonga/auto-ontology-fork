// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useEffect, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import {
	chatDetailsApi,
	sqlQueryTimeoutApi,
	sqlResultMaxRowsApi,
	visualizationApi,
} from '@/api/settings';
import { Button } from '@/common/Button';
import { Toast } from '@/common/Toast';
import { Toggle } from '@/common/Toggle/Toggle';
import { setChatDetailsExpanded } from '@/lib/queries/chatDisplay';
import { ButtonTheme, Size } from '@/enums/button';
import { ToastVariant } from '@/enums/toast';
import {
	DEFAULT_SQL_QUERY_TIMEOUT_SECONDS,
	MAX_SQL_QUERY_TIMEOUT_SECONDS,
	MIN_SQL_QUERY_TIMEOUT_SECONDS,
	isValidSqlQueryTimeout,
} from '@/lib/sqlQueryTimeout';
import {
	DEFAULT_SQL_RESULT_MAX_ROWS,
	MAX_SQL_RESULT_MAX_ROWS,
	MIN_SQL_RESULT_MAX_ROWS,
	isValidSqlResultMaxRows,
} from '@/lib/sqlResultMaxRows';

export default function AgentSettingsPage() {
	// Defaults to on (matches a missing `visualization_enabled` configuration)
	// until the server value is fetched, so the switch never flashes "off" first.
	const [visualization, setVisualization] = useState(true);
	const [saving, setSaving] = useState(false);
	const [error, setError] = useState<string | null>(null);

	// Defaults to off, matching a missing `chat_details_expanded` configuration.
	const queryClient = useQueryClient();
	const [detailsExpanded, setDetailsExpanded] = useState(false);
	const [savingDetails, setSavingDetails] = useState(false);

	// `timeoutSeconds` is what the server holds; `timeoutDraft` is the raw input text.
	const [timeoutSeconds, setTimeoutSeconds] = useState(DEFAULT_SQL_QUERY_TIMEOUT_SECONDS);
	const [timeoutDraft, setTimeoutDraft] = useState(String(DEFAULT_SQL_QUERY_TIMEOUT_SECONDS));
	const [savingTimeout, setSavingTimeout] = useState(false);

	const draftSeconds = Number(timeoutDraft);
	const draftValid = timeoutDraft.trim() !== '' && isValidSqlQueryTimeout(draftSeconds);
	const timeoutDirty = draftValid && draftSeconds !== timeoutSeconds;

	// Same shape as the timeout: `rowLimit` is the server value, `rowLimitDraft` the input text.
	const [rowLimit, setRowLimit] = useState(DEFAULT_SQL_RESULT_MAX_ROWS);
	const [rowLimitDraft, setRowLimitDraft] = useState(String(DEFAULT_SQL_RESULT_MAX_ROWS));
	const [savingRowLimit, setSavingRowLimit] = useState(false);

	const draftRows = Number(rowLimitDraft);
	const rowLimitValid = rowLimitDraft.trim() !== '' && isValidSqlResultMaxRows(draftRows);
	const rowLimitDirty = rowLimitValid && draftRows !== rowLimit;

	useEffect(() => {
		let cancelled = false;
		visualizationApi.get().then((res) => {
			if (cancelled || res.error) return;
			setVisualization(res.enabled);
		});
		chatDetailsApi.get().then((res) => {
			if (cancelled || res.error) return;
			setDetailsExpanded(res.enabled);
		});
		sqlQueryTimeoutApi.get().then((res) => {
			if (cancelled || res.error) return;
			setTimeoutSeconds(res.seconds);
			setTimeoutDraft(String(res.seconds));
		});
		sqlResultMaxRowsApi.get().then((res) => {
			if (cancelled || res.error) return;
			setRowLimit(res.rows);
			setRowLimitDraft(String(res.rows));
		});
		return () => {
			cancelled = true;
		};
	}, []);

	const handleSaveTimeout = async () => {
		if (savingTimeout || !timeoutDirty) return;
		setSavingTimeout(true);
		setError(null);

		const res = await sqlQueryTimeoutApi.set(draftSeconds);
		setSavingTimeout(false);

		if (res.error) {
			setError(res.message ?? 'Failed to update setting. Please try again.');
			return;
		}
		setTimeoutSeconds(res.seconds);
		setTimeoutDraft(String(res.seconds));
	};

	const handleSaveRowLimit = async () => {
		if (savingRowLimit || !rowLimitDirty) return;
		setSavingRowLimit(true);
		setError(null);

		const res = await sqlResultMaxRowsApi.set(draftRows);
		setSavingRowLimit(false);

		if (res.error) {
			setError(res.message ?? 'Failed to update setting. Please try again.');
			return;
		}
		setRowLimit(res.rows);
		setRowLimitDraft(String(res.rows));
	};

	const handleToggle = async () => {
		if (saving) return;
		const next = !visualization;
		setSaving(true);
		setError(null);
		setVisualization(next);

		const res = await visualizationApi.setEnabled(next);
		setSaving(false);

		if (res.error) {
			setVisualization(!next); // roll back the optimistic update
			setError(res.message ?? 'Failed to update setting. Please try again.');
		}
	};

	const handleDetailsToggle = async (next: boolean) => {
		if (savingDetails) return;
		setSavingDetails(true);
		setError(null);
		setDetailsExpanded(next);

		const res = await chatDetailsApi.setEnabled(next);
		setSavingDetails(false);

		if (res.error) {
			setDetailsExpanded(!next); // roll back the optimistic update
			setError(res.message ?? 'Failed to update setting. Please try again.');
			return;
		}
		setChatDetailsExpanded(queryClient, res.enabled);
	};

	return (
		<main className="min-h-0 min-w-0 flex-1 overflow-y-auto px-7 py-6 sm:px-10 sm:py-7">
			<div className="w-full space-y-5">
				<h1 className="text-[28px] font-bold text-glass-text dark:text-zinc-50 leading-tight tracking-tight">
					Agent Settings
				</h1>

				<div className="border p-4 border-white/70 dark:border-white/10 rounded-card glass">
					<div className="flex items-center justify-between gap-4">
						<div className="min-w-0">
							<h2 className="text-sm font-semibold text-heading dark:text-zinc-100">
								Visualize SQL Results
							</h2>
							<p className="mt-1 text-xs text-secondary dark:text-zinc-400">
								Auto-generate visualizations for SQL results, for all users
							</p>
						</div>
						<button
							type="button"
							role="switch"
							aria-checked={visualization}
							aria-label="Visualize SQL Results"
							disabled={saving}
							onClick={() => {
								void handleToggle();
							}}
							className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer items-center rounded-full transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
								visualization ? 'bg-primary' : 'bg-zinc-300 dark:bg-zinc-600'
							}`}
						>
							<span
								className={`inline-block h-5 w-5 transform rounded-full bg-white shadow transition-transform ${
									visualization ? 'translate-x-5' : 'translate-x-0.5'
								}`}
							/>
						</button>
					</div>
				</div>

				<div className="border p-4 border-white/70 dark:border-white/10 rounded-card glass">
					<div className="flex items-center justify-between gap-4">
						<div className="min-w-0">
							<h2 className="text-sm font-semibold text-heading dark:text-zinc-100">
								Expand Reasoning and SQL
							</h2>
							<p className="mt-1 text-xs text-secondary dark:text-zinc-400">
								Show each answer&apos;s explanation and SQL expanded in chat, for
								all users. When off they start collapsed, one click away.
							</p>
						</div>
						<Toggle
							checked={detailsExpanded}
							aria-label="Expand Reasoning and SQL"
							disabled={savingDetails}
							onChange={(next) => {
								void handleDetailsToggle(next);
							}}
						/>
					</div>
				</div>

				<div className="border p-4 border-white/70 dark:border-white/10 rounded-card glass">
					<form
						className="flex items-center justify-between gap-4"
						onSubmit={(e) => {
							e.preventDefault();
							void handleSaveTimeout();
						}}
					>
						<div className="min-w-0">
							<label
								htmlFor="sql-query-timeout"
								className="text-sm font-semibold text-heading dark:text-zinc-100"
							>
								SQL Query Timeout
							</label>
							<p className="mt-1 text-xs text-secondary dark:text-zinc-400">
								Seconds a query generated by the agent may run before it is
								cancelled ({MIN_SQL_QUERY_TIMEOUT_SECONDS}–
								{MAX_SQL_QUERY_TIMEOUT_SECONDS}, default{' '}
								{DEFAULT_SQL_QUERY_TIMEOUT_SECONDS})
							</p>
						</div>
						<div className="flex shrink-0 items-center gap-2">
							<input
								id="sql-query-timeout"
								type="number"
								inputMode="numeric"
								min={MIN_SQL_QUERY_TIMEOUT_SECONDS}
								max={MAX_SQL_QUERY_TIMEOUT_SECONDS}
								step={1}
								value={timeoutDraft}
								disabled={savingTimeout}
								aria-invalid={!draftValid}
								onChange={(e) => setTimeoutDraft(e.target.value)}
								className="h-9 w-24 rounded-xl border border-zinc-900/15 bg-white/70 px-3 text-sm text-body outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/30 disabled:cursor-not-allowed disabled:bg-zinc-100 disabled:text-disabled aria-[invalid=true]:border-red-500 dark:border-white/15 dark:bg-white/[0.06] dark:text-zinc-300 dark:disabled:bg-zinc-800/50 dark:disabled:text-zinc-400"
							/>
							<Button
								theme={ButtonTheme.Primary}
								size={Size.REGULAR}
								type="submit"
								disabled={savingTimeout || !timeoutDirty}
							>
								{savingTimeout ? 'Saving…' : 'Save'}
							</Button>
						</div>
					</form>
				</div>

				<div className="border p-4 border-white/70 dark:border-white/10 rounded-card glass">
					<form
						className="flex items-center justify-between gap-4"
						onSubmit={(e) => {
							e.preventDefault();
							void handleSaveRowLimit();
						}}
					>
						<div className="min-w-0">
							<label
								htmlFor="sql-result-max-rows"
								className="text-sm font-semibold text-heading dark:text-zinc-100"
							>
								SQL Result Row Limit
							</label>
							<p className="mt-1 text-xs text-secondary dark:text-zinc-400">
								Most rows one query generated by the agent may return. A longer
								result is cut, and the answer says it was limited so the figures are
								never presented as complete (
								{MIN_SQL_RESULT_MAX_ROWS.toLocaleString('en-US')}–
								{MAX_SQL_RESULT_MAX_ROWS.toLocaleString('en-US')}, default{' '}
								{DEFAULT_SQL_RESULT_MAX_ROWS.toLocaleString('en-US')})
							</p>
						</div>
						<div className="flex shrink-0 items-center gap-2">
							<input
								id="sql-result-max-rows"
								type="number"
								inputMode="numeric"
								min={MIN_SQL_RESULT_MAX_ROWS}
								max={MAX_SQL_RESULT_MAX_ROWS}
								step={1}
								value={rowLimitDraft}
								disabled={savingRowLimit}
								aria-invalid={!rowLimitValid}
								onChange={(e) => setRowLimitDraft(e.target.value)}
								className="h-9 w-28 rounded-xl border border-zinc-900/15 bg-white/70 px-3 text-sm text-body outline-none transition-colors focus:border-primary focus:ring-2 focus:ring-primary/30 disabled:cursor-not-allowed disabled:bg-zinc-100 disabled:text-disabled aria-[invalid=true]:border-red-500 dark:border-white/15 dark:bg-white/[0.06] dark:text-zinc-300 dark:disabled:bg-zinc-800/50 dark:disabled:text-zinc-400"
							/>
							<Button
								theme={ButtonTheme.Primary}
								size={Size.REGULAR}
								type="submit"
								disabled={savingRowLimit || !rowLimitDirty}
							>
								{savingRowLimit ? 'Saving…' : 'Save'}
							</Button>
						</div>
					</form>
				</div>
			</div>

			<Toast
				open={error !== null}
				message={error ?? ''}
				variant={ToastVariant.Error}
				onClose={() => setError(null)}
			/>
		</main>
	);
}
