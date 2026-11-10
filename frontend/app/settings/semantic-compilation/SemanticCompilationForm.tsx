// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useEffect, useState } from 'react';
import { distinctValueProbingApi, semanticCompilationApi } from '@/api/settings';
import { formatDate } from '@/common/date';
import { Icon, IconName } from '@/common/icons';
import { ConfirmModal } from '@/common/modal';
import { Spinner } from '@/common/Spinner';
import { Toast } from '@/common/Toast';
import { Toggle } from '@/common/Toggle';
import { ToastVariant } from '@/enums/toast';
import {
	describeIndexEntry,
	describeReindexResult,
	reindexErrorMessage,
} from '@/lib/semanticIndex';
import type { SemanticCompilationStatus, SemanticIndexEntry } from '@/types/semanticIndex';

export const SemanticCompilationForm = ({
	initialEnabled,
	initialProbingEnabled,
	hasDatabases,
	canManage,
}: {
	initialEnabled: boolean;
	initialProbingEnabled: boolean;
	hasDatabases: boolean;
	/** Holds `semanticCompilation: manage`, which the reindex route requires. */
	canManage: boolean;
}) => {
	// Seeded from the server (see page.tsx) so the correct state renders on first
	// paint; updated optimistically and rolled back if the save fails.
	const [enabled, setEnabled] = useState(initialEnabled);
	const [probingEnabled, setProbingEnabled] = useState(initialProbingEnabled);
	const [savingProbing, setSavingProbing] = useState(false);
	const [saving, setSaving] = useState(false);
	const [confirmModalOpen, setConfirmModalOpen] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const [message, setMessage] = useState<string | null>(null);
	const [running, setRunning] = useState(false);
	const [lastSuccessAt, setLastSuccessAt] = useState<string | null>(null);
	const [lastFailureAt, setLastFailureAt] = useState<string | null>(null);
	// Undefined `index` (older backend) renders no index section at all.
	const [indexEntries, setIndexEntries] = useState<SemanticIndexEntry[]>([]);
	const [rebuilding, setRebuilding] = useState<string | null>(null);
	const applyStatus = (result: SemanticCompilationStatus) => {
		setRunning(result.running);
		setLastSuccessAt(result.last_success_at);
		setLastFailureAt(result.last_failure_at);
		setIndexEntries(result.index ?? []);
	};
	useEffect(() => {
		semanticCompilationApi
			.getStatus()
			.then(applyStatus)
			.catch(() => {});
	}, []);
	// Only set when it's more recent than the last success (see history.py),
	// so a stale failure never outlives the run that fixed it.
	const failed = !running && lastFailureAt !== null;
	// Whether semantic_compilation_history has anything to report yet. A newly
	// enabled instance that hasn't finished its first pass has none of these,
	// so there's nothing meaningful to show — surfacing "ready" with a "—"
	// placeholder would be misleading.
	const hasHistory = running || lastSuccessAt !== null || lastFailureAt !== null;

	const handleToggle = async () => {
		if (saving) return;
		const next = !enabled;
		setSaving(true);
		setError(null);
		setMessage(null);
		setEnabled(next);

		try {
			const result = await semanticCompilationApi.setEnabled(next);
			setEnabled(result.enabled);
			setMessage(
				result.enabled
					? 'Semantic compilation enabled — a compilation run has been triggered.'
					: 'Semantic compilation disabled.',
			);
			if (result.enabled) {
				// Enabling triggers a run immediately (see the PUT route), so by the
				// time this resolves the ingestion service has already inserted a
				// history row and is very likely already running — refresh status
				// so the section appears now instead of only on the next page load.
				semanticCompilationApi
					.getStatus()
					.then(applyStatus)
					.catch(() => {});
			}
		} catch {
			setEnabled(!next); // roll back the optimistic update
			setError('Failed to update semantic compilation. Please try again.');
		} finally {
			setSaving(false);
		}
	};

	const handleProbingToggle = async () => {
		if (savingProbing) return;
		const next = !probingEnabled;
		setSavingProbing(true);
		setError(null);
		setMessage(null);
		setProbingEnabled(next);

		try {
			const result = await distinctValueProbingApi.setEnabled(next);
			setProbingEnabled(result.enabled);
			// No run is triggered: the flag is read at the start of each run, so
			// say when it takes effect rather than implying something happened now.
			setMessage(
				result.enabled
					? 'Distinct value scanning enabled — applies from the next compilation run.'
					: 'Distinct value scanning disabled — applies from the next compilation run.',
			);
		} catch {
			setProbingEnabled(!next); // roll back the optimistic update
			setError('Failed to update distinct value scanning. Please try again.');
		} finally {
			setSavingProbing(false);
		}
	};

	const handleRebuild = async (databaseName: string) => {
		if (rebuilding !== null) return;
		setRebuilding(databaseName);
		setError(null);
		setMessage(null);

		try {
			const outcome = await semanticCompilationApi.reindex(databaseName);
			if (!outcome.ok) {
				setError(reindexErrorMessage(outcome.status, outcome.detail));
			} else {
				const { complete, message: text } = describeReindexResult(outcome.result);
				// A partial rebuild is not a success: show it as an error so it is not missed.
				if (complete) setMessage(text);
				else setError(text);
			}
		} catch {
			setError('Failed to reach the server. Please try again.');
		}
		// Refresh either way: a refused or partial run may still have changed the counts.
		await semanticCompilationApi
			.getStatus()
			.then(applyStatus)
			.catch(() => {});
		setRebuilding(null);
	};

	const handleReset = () => {
		setError(null);
		setConfirmModalOpen(false);
		// The ingestion service deletes in the background and answers 202, so the
		// request only starts the reset. Nothing here depends on the response, so
		// it is left unawaited and only a failure to reach the API is surfaced.
		setMessage('Semantic layer reset started — it runs in the background.');
		semanticCompilationApi.reset().catch(() => {
			setMessage(null);
			setError('Failed to reset the semantic layer. Please try again.');
		});
	};

	return (
		<div className="h-full overflow-auto p-6">
			<div className="mx-auto max-w-xl">
				<h1 className="mb-1 text-[28px] font-bold text-glass-text dark:text-zinc-50 leading-tight tracking-tight">
					Semantic Compilation
				</h1>
				<p className="mb-4 text-xs text-secondary">
					When enabled, the ingestion service compiles the semantic layer for every
					connected database on startup and once every 24 hours. Enabling it also triggers
					a compilation run immediately.
				</p>

				{!hasDatabases && (
					<div className="mb-3 flex items-center rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-700">
						<span className="text-sm font-medium text-heading dark:text-zinc-100">
							Please connect any data source
						</span>
					</div>
				)}

				{hasDatabases && (
					<>
						{enabled && hasHistory && (
							<div className="mb-3 flex items-center justify-between rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-700">
								<div className="flex flex-col">
									<span className="text-sm font-medium text-heading dark:text-zinc-100">
										{running && 'Semantic compilation is running'}
										{!running && failed && 'Semantic compilation failed'}
										{!running && !failed && 'Semantic compilation is ready'}
									</span>
									<span className="text-xs text-secondary">
										{failed && !running
											? `Last attempt: ${formatDate(lastFailureAt as string, 'MMM DD YYYY, HH:mm Z')}`
											: `Last semantic compilation: ${
													lastSuccessAt
														? formatDate(
																lastSuccessAt,
																'MMM DD YYYY, HH:mm Z',
															)
														: '—'
												}`}
									</span>
								</div>
								{running ? (
									<span title="Semantic Compilation Is Running">
										<Spinner className="h-4 w-4 shrink-0 text-primary" />
									</span>
								) : failed ? (
									<span
										className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-red-500/15 text-red-600 dark:text-red-400"
										title="Semantic Compilation Failed"
									>
										<Icon name={IconName.Close} className="h-3 w-3" />
									</span>
								) : (
									<span
										className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-primary/15 text-primary"
										title="Semantic Compilation Is Ready"
									>
										<Icon name={IconName.Check} className="h-3 w-3" />
									</span>
								)}
							</div>
						)}

						{indexEntries.length > 0 && (
							<div className="mb-3 rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-700">
								<span className="text-sm font-medium text-heading dark:text-zinc-100">
									Semantic Index
								</span>
								<p className="text-xs text-secondary">
									The embeddings chat uses to find Terms and columns. Rebuild one
									if entries are missing, for example after the embedding endpoint
									was failing during ingestion.
								</p>
								<ul className="mt-2 space-y-2">
									{indexEntries.map((entry) => {
										const { complete, label } = describeIndexEntry(entry);
										const busy = rebuilding === entry.database_name;
										return (
											<li
												key={entry.database_name}
												className="flex items-center justify-between gap-3"
											>
												<div className="flex min-w-0 flex-col">
													<span className="truncate text-sm text-heading dark:text-zinc-100">
														{entry.database_name}
													</span>
													<span
														className={
															complete
																? 'text-xs text-secondary'
																: 'text-xs font-medium text-amber-700 dark:text-amber-400'
														}
													>
														{label}
													</span>
												</div>
												<button
													type="button"
													disabled={
														!canManage || running || rebuilding !== null
													}
													title={
														canManage
															? running
																? 'A compilation is running'
																: undefined
															: 'Requires the manage permission'
													}
													onClick={() => {
														void handleRebuild(entry.database_name);
													}}
													className="flex shrink-0 cursor-pointer items-center gap-2 rounded-lg border border-zinc-300 px-3 py-1.5 text-sm font-medium text-heading transition-colors hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-zinc-600 dark:text-zinc-100 dark:hover:bg-zinc-800"
												>
													{busy && <Spinner className="h-3 w-3" />}
													{busy ? 'Rebuilding…' : 'Rebuild index'}
												</button>
											</li>
										);
									})}
								</ul>
							</div>
						)}

						{!enabled && (
							<div className="mb-3 flex items-center rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-700">
								<span className="text-sm font-medium text-heading dark:text-zinc-100">
									You can enable the semantic compilation
								</span>
							</div>
						)}

						{/* Above the compilation toggle on purpose: this is a property of
						    how compilation runs, so it can be set before turning
						    compilation on, and stays editable afterwards. */}
						<div className="mb-3 flex items-center justify-between rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-700">
							<div className="flex flex-col pr-4">
								<span className="text-sm font-medium text-heading dark:text-zinc-100">
									Distinct Value Scanning
								</span>
								<span className="text-xs text-secondary">
									{probingEnabled
										? 'Scans low-cardinality text columns for their full value set, so rare categories are captured too.'
										: 'Off — columns keep only the values seen in each table’s row sample.'}
								</span>
							</div>
							<Toggle
								checked={probingEnabled}
								aria-label="Distinct Value Scanning"
								disabled={savingProbing}
								onChange={handleProbingToggle}
							/>
						</div>

						<div className="flex items-center justify-between rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-700">
							<div className="flex flex-col">
								<span className="text-sm font-medium text-heading dark:text-zinc-100">
									Enable Semantic Compilation
								</span>
								<span className="text-xs text-secondary">
									{enabled ? 'Running on the 24h schedule.' : 'Currently off.'}
								</span>
							</div>
							<Toggle
								checked={enabled}
								aria-label="Enable Semantic Compilation"
								disabled={saving}
								onChange={handleToggle}
							/>
						</div>

						{enabled && (
							<div className="mt-3 flex items-center justify-between rounded-lg border border-zinc-200 px-4 py-3 dark:border-zinc-700">
								<div className="flex flex-col">
									<span className="text-sm font-medium text-heading dark:text-zinc-100">
										Reset Semantic Layer
									</span>
									<span className="text-xs text-secondary">
										Deletes the semantic layer for every database, and rebuild
										it from scratch.
									</span>
								</div>
								<button
									type="button"
									disabled={saving}
									onClick={() => setConfirmModalOpen(true)}
									className="cursor-pointer rounded-lg border border-red-300 px-3 py-1.5 text-sm font-medium text-red-700 transition-colors hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-red-900/60 dark:text-red-300 dark:hover:bg-red-950/40"
								>
									Reset
								</button>
							</div>
						)}
					</>
				)}
			</div>

			<ConfirmModal
				open={confirmModalOpen}
				title="Reset Semantic Layer"
				message="This deletes the semantic layer and a rebuild will be triggered."
				confirmLabel="Reset"
				onConfirm={handleReset}
				onCancel={() => setConfirmModalOpen(false)}
			/>

			<Toast
				open={error !== null}
				message={error ?? ''}
				variant={ToastVariant.Error}
				onClose={() => setError(null)}
			/>
			<Toast
				open={message !== null}
				message={message ?? ''}
				variant={ToastVariant.Success}
				onClose={() => setMessage(null)}
			/>
		</div>
	);
};
