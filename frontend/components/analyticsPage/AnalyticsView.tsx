// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useEffect, useState } from 'react';
import { Button } from '@/common/Button';
import { Pagination } from '@/common/Pagination';
import { EmptyState } from '@/common/EmptyState';
import { Size, ButtonTheme } from '@/enums/button';
import { ToastVariant } from '@/enums/toast';
import { Icon, IconName } from '@/common/icons';
import { SkeletonCard } from '@/common/Skeleton';
import { SqlBlock } from '@/common/SqlBlock';
import { Toast } from '@/common/Toast';
import { analyticsApi } from '@/api/analytics';
import { formatDate } from '@/common/date';
import { usePagination } from '@/hooks/usePagination';
import type { ConversationAnalytics } from '@/types/analytics';

const CSV_HEADERS = ['Timestamp', 'User', 'Source', 'Question', 'Reasoning', 'SQL'];
const escapeCsv = (value: string) => `"${value.replace(/"/g, '""')}"`;

/** "4.2 s", "1 min 05 s": how long the answer took, when both times are known. */
const formatResponseTime = (row: ConversationAnalytics): string | null => {
	if (!row.response_timestamp) return null;
	const ms = Date.parse(row.response_timestamp) - Date.parse(row.question_timestamp);
	if (!Number.isFinite(ms) || ms < 0) return null;
	const seconds = ms / 1000;
	if (seconds < 60) return `${seconds.toFixed(1)} s`;
	const minutes = Math.floor(seconds / 60);
	return `${minutes} min ${String(Math.round(seconds % 60)).padStart(2, '0')} s`;
};

const pillClass = 'shrink-0 rounded-full px-2.5 py-0.5 text-[11px] font-semibold';

/** One question asked in chat: what was asked, by whom, and what came back. */
const AnalyticsCard = ({ row }: { row: ConversationAnalytics }) => {
	const [open, setOpen] = useState(false);
	const who = row.user.name || row.user.email;
	const responseTime = formatResponseTime(row);
	const hasDetails = Boolean(row.response?.trim() || row.sql?.trim());
	const detailsId = `analytics-${row.id}-details`;

	return (
		<li className="glass flex flex-col gap-3 rounded-card border border-white/70 p-5 dark:border-white/10">
			<div className="flex items-start gap-3">
				<p className="line-clamp-2 min-w-0 flex-1 text-[15px] leading-snug font-semibold text-glass-text dark:text-zinc-50">
					{row.question}
				</p>
				{responseTime ? (
					<span
						className={`${pillClass} bg-zinc-900/5 text-body tabular-nums dark:bg-white/10 dark:text-zinc-300`}
						title="Time to answer"
					>
						{responseTime}
					</span>
				) : null}
				{row.sql?.trim() ? (
					<span
						className={`${pillClass} bg-primary/10 text-primary-strong dark:bg-primary/20 dark:text-primary-soft`}
					>
						SQL
					</span>
				) : (
					<span
						className={`${pillClass} bg-zinc-900/5 text-secondary dark:bg-white/10 dark:text-zinc-400`}
					>
						No SQL
					</span>
				)}
			</div>

			<div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-xs text-secondary dark:text-zinc-400">
				<span className="flex h-5 w-5 items-center justify-center rounded-full bg-primary text-[10px] font-bold text-white">
					{who.charAt(0).toUpperCase()}
				</span>
				<span className="max-w-56 truncate text-body dark:text-zinc-300">{who}</span>
				<span aria-hidden>·</span>
				<span className="font-semibold uppercase">{row.source || '—'}</span>
				<span aria-hidden>·</span>
				<time dateTime={row.question_timestamp}>
					{formatDate(row.question_timestamp, 'MMM D, YYYY · HH:mm')}
				</time>
			</div>

			{hasDetails && (
				<>
					<button
						type="button"
						aria-expanded={open}
						aria-controls={detailsId}
						onClick={() => setOpen((o) => !o)}
						className="inline-flex items-center gap-1.5 self-start rounded text-xs font-medium text-secondary transition-colors hover:text-primary focus-visible:ring-2 focus-visible:ring-primary focus-visible:outline-none dark:text-zinc-400 dark:hover:text-primary-soft"
					>
						<Icon
							name={IconName.ChevronRight}
							className={`h-3 w-3 transition-transform ${open ? 'rotate-90' : ''}`}
						/>
						{open ? 'Hide reasoning and SQL' : 'Show reasoning and SQL'}
					</button>
					{open && (
						<div id={detailsId} className="flex flex-col gap-3">
							{row.response?.trim() ? (
								<p className="text-sm leading-relaxed whitespace-pre-wrap text-body dark:text-zinc-300">
									{row.response}
								</p>
							) : null}
							{row.sql?.trim() ? <SqlBlock sql={row.sql} /> : null}
						</div>
					)}
				</>
			)}
		</li>
	);
};

const rangeText = ({
	firstItemIndex,
	lastItemIndex,
	totalItems,
}: {
	firstItemIndex: number;
	lastItemIndex: number;
	totalItems: number;
}) => `${firstItemIndex}–${lastItemIndex} of ${totalItems}`;

export const AnalyticsView = () => {
	const [rows, setRows] = useState<ConversationAnalytics[]>([]);
	const [total, setTotal] = useState(0);
	const [loading, setLoading] = useState(true);
	const [downloading, setDownloading] = useState(false);
	const [error, setError] = useState<string | null>(null);
	// Kept apart from `error`: the table below only renders while `error` is
	// null, so a failed export must not take the rows already on screen with it.
	const [downloadError, setDownloadError] = useState<string | null>(null);
	const { skip, pageSize, pagination } = usePagination({ totalItems: total });

	useEffect(() => {
		let cancelled = false;

		(async () => {
			setLoading(true);
			const res = await analyticsApi.list({ skip, limit: pageSize });
			if (cancelled) return;
			if (res.error) {
				// The page already on screen is left alone: dropping the total
				// would collapse the pager to page one and send the request for
				// it, so a single failed request would cost two.
				setError(res.message ?? 'Failed to load analytics');
			} else {
				setError(null);
				setRows(res.data ?? []);
				setTotal(res.total);
			}
			setLoading(false);
		})();

		return () => {
			cancelled = true;
		};
	}, [skip, pageSize]);

	const downloadRows = (analytics: ConversationAnalytics[]) => {
		const lines = [
			CSV_HEADERS.join(','),
			...analytics.map((row) =>
				[
					formatDate(row.question_timestamp),
					row.user.name || row.user.email,
					row.source ?? '',
					row.question ?? '',
					row.response ?? '',
					row.sql ?? '',
				]
					.map(escapeCsv)
					.join(','),
			),
		];
		const csv = lines.join('\r\n');

		const blob = new Blob([`\uFEFF${csv}`], { type: 'text/csv;charset=utf-8;' });
		const url = URL.createObjectURL(blob);
		const filename = `auto-ontology-analytics-${formatDate(Date.now(), 'YYYY-MM')}.csv`;
		const anchor = document.createElement('a');
		anchor.href = url;
		anchor.download = filename;
		document.body.appendChild(anchor);
		anchor.click();
		anchor.remove();
		URL.revokeObjectURL(url);
	};

	const handleDownload = async () => {
		setDownloading(true);
		setDownloadError(null);
		const res = await analyticsApi.list();
		setDownloading(false);
		if (res.error) {
			setDownloadError(res.message ?? 'Failed to download analytics');
			return;
		}
		downloadRows(res.data ?? []);
	};

	return (
		<div className="flex h-full w-full flex-col">
			<header className="flex flex-wrap items-end gap-4 px-6 pt-8 pb-2 sm:px-10">
				<div className="flex min-w-0 flex-col gap-1.5">
					<h1 className="text-[28px] leading-tight font-bold tracking-tight text-glass-text dark:text-zinc-50">
						Analytics
					</h1>
					<p className="text-sm text-secondary dark:text-zinc-400">
						{total === 1
							? '1 question asked in chat'
							: `${total} questions asked in chat`}
					</p>
				</div>
				<div className="ml-auto">
					<Button
						theme={ButtonTheme.Primary}
						size={Size.REGULAR}
						type="button"
						onClick={handleDownload}
						disabled={total === 0 || downloading}
						shadow
					>
						{downloading ? 'Preparing download…' : 'Download'}
					</Button>
				</div>
			</header>

			<div className="flex-1 overflow-y-auto px-6 py-6 sm:px-10">
				{loading && (
					<div
						role="status"
						aria-label="Loading analytics"
						className="flex flex-col gap-3"
					>
						{Array.from({ length: 5 }).map((_, i) => (
							<SkeletonCard key={i} rows={2} />
						))}
					</div>
				)}

				{!loading && error == null && total === 0 && (
					<EmptyState
						icon={IconName.ChartLine}
						title="No Analytics Recorded Yet"
						description="Analytics are captured automatically when you send messages in a conversation."
					/>
				)}

				{!loading && error == null && rows.length > 0 && (
					<div className="flex flex-col gap-3">
						<ul className="flex flex-col gap-3">
							{rows.map((row) => (
								<AnalyticsCard key={row.id} row={row} />
							))}
						</ul>
						<Pagination
							totalItems={pagination.totalItems}
							pageSize={pagination.pageSize}
							page={pagination.page}
							onPageChange={pagination.onPageChange}
							rangeTextFormatFn={rangeText}
							className="glass rounded-card border border-white/70 px-4 py-3 dark:border-white/10"
						/>
					</div>
				)}
			</div>

			<Toast
				open={error !== null}
				message={error ?? ''}
				title="Couldn't load analytics"
				variant={ToastVariant.Error}
				onClose={() => setError(null)}
			/>

			<Toast
				open={downloadError !== null}
				message={downloadError ?? ''}
				title="Couldn't download analytics"
				variant={ToastVariant.Error}
				onClose={() => setDownloadError(null)}
			/>
		</div>
	);
};
