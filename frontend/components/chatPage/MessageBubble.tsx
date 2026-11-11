// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useId, useMemo, useState } from 'react';
import type { ChatMessage, ClarificationReply } from '@/types/chat';
import { CopyButton } from '@/common/Button';
import { formatDate } from '@/common/date';
import { Icon, IconName } from '@/common/icons';
import { parseSqlResponse, type ParsedTable } from '@/lib/parseSqlResponse';
import {
	SqlBlock,
	codeHeaderClass,
	codeLabelClass,
	codeSurfaceClass,
	codeTextClass,
} from '@/common/SqlBlock';
import { ClarificationCard } from './ClarificationCard';
import { DynamicTable } from './DynamicTable';
import { FormattedContent } from './FormattedContent';
import { glassFocus, glassSurface } from './glass';

type SingleCellResult = { column: string; value: string };

type QueryResultsSectionProps = {
	sqlResponse: string | undefined;
	parsedTable: ParsedTable | null;
	singleCell: SingleCellResult | null;
	className?: string;
};

const QueryResultsSection = ({
	sqlResponse,
	parsedTable,
	singleCell,
	className,
}: QueryResultsSectionProps) => (
	<div className={className}>
		<div className="mb-1 text-xs font-medium text-secondary dark:text-zinc-400">
			Query results
		</div>
		{!sqlResponse && <p className="text-xs italic text-secondary dark:text-zinc-500">null</p>}
		{sqlResponse && singleCell && (
			// A single figure is the whole answer, so it gets a card of its own.
			<div
				className={`inline-flex min-w-48 flex-col gap-1 rounded-2xl px-5 py-4 ${glassSurface}`}
			>
				<span className="text-[11px] font-semibold tracking-wide text-secondary uppercase dark:text-zinc-400">
					{singleCell.column}
				</span>
				<span className="text-2xl font-semibold text-glass-text tabular-nums dark:text-zinc-50">
					{singleCell.value}
				</span>
			</div>
		)}
		{sqlResponse && parsedTable && !singleCell && <DynamicTable table={parsedTable} />}
		{sqlResponse && !parsedTable && (
			<div className={`group relative overflow-hidden rounded-xl ${codeSurfaceClass}`}>
				<div className={codeHeaderClass}>
					<span className={codeLabelClass}>Raw</span>
					<CopyButton text={sqlResponse} />
				</div>
				<pre
					className={`overflow-x-auto p-3 font-mono text-xs leading-relaxed ${codeTextClass}`}
				>
					<code>{sqlResponse}</code>
				</pre>
			</div>
		)}
	</div>
);

type MessageBubbleProps = {
	message: ChatMessage;
	/**
	 * Whether an answer's explanation and SQL start expanded (Settings > Agent
	 * Settings). The user can still toggle each answer.
	 */
	detailsExpandedByDefault?: boolean;
	/** Whether this is the newest message; only then can a clarification still be answered. */
	isLatest?: boolean;
	/** A turn is streaming, so a clarification answer cannot be sent yet. */
	isRunning?: boolean;
	onAnswerClarification?: (text: string, reply: ClarificationReply) => Promise<boolean>;
};

/** True when the assistant message embeds a ResultChart fence (illumex Message 2). */
const hasEmbeddedChart = (content: string): boolean =>
	/(^|\n)```(?:chart|chart-carousel)\b/.test(content);

export const MessageBubble = ({
	message,
	detailsExpandedByDefault = false,
	isLatest = false,
	isRunning = false,
	onAnswerClarification,
}: MessageBubbleProps) => {
	const isUser = message.role === 'user';
	// `null` until the user clicks, so the bubble follows the setting — which
	// may load after the bubble mounts — rather than freezing its first value.
	const [detailsOpen, setDetailsOpen] = useState<boolean | null>(null);
	const detailsId = useId();
	const parsedTable = useMemo(() => parseSqlResponse(message.sqlResponse), [message.sqlResponse]);

	const singleCellResult = useMemo<SingleCellResult | null>(() => {
		if (!parsedTable) return null;
		if (parsedTable.rows.length !== 1 || parsedTable.columns.length !== 1) return null;
		const [column] = parsedTable.columns;
		return { column, value: parsedTable.rows[0][column] ?? '' };
	}, [parsedTable]);

	// Illumex layout:
	// Message 1 — text + SQL (no table)
	// Message 2 — charts only, OR table only
	const showCharts = !isUser && hasEmbeddedChart(message.content);
	const showSql = !isUser && Boolean(message.sql) && !showCharts;
	const showQueryResults = !isUser && !showCharts && Boolean(message.sqlResponse);
	const showText = Boolean(message.content.trim());

	// The explanation and SQL of an answer fold behind one toggle. Only a bubble
	// that carries SQL folds: plain replies and errors stay readable as they are,
	// and the results (Message 2) are a separate bubble that always shows.
	const collapsible = showSql;
	const detailsExpanded = !collapsible || (detailsOpen ?? detailsExpandedByDefault);

	if (message.clarification) {
		return (
			<div className="w-full">
				<ClarificationCard
					part={message.clarification}
					isLatest={isLatest}
					disabled={isRunning || !onAnswerClarification}
					onAnswer={onAnswerClarification ?? (async () => false)}
				/>
			</div>
		);
	}

	// Chat-app layout: the question sits in a bubble on the right, the answer
	// reads as plain text across the whole column.
	return (
		<div className={`flex ${isUser ? 'justify-end' : 'justify-start'}`}>
			<div
				className={
					isUser
						? `max-w-[80%] rounded-3xl px-4 py-2.5 text-glass-text dark:text-zinc-100 ${glassSurface}`
						: 'w-full text-heading dark:text-zinc-100'
				}
			>
				{collapsible && (
					<button
						type="button"
						aria-expanded={detailsExpanded}
						aria-controls={detailsId}
						onClick={() => setDetailsOpen(!detailsExpanded)}
						className={`flex cursor-pointer items-center gap-1.5 rounded text-xs font-medium text-secondary transition-colors hover:text-primary dark:text-zinc-400 dark:hover:text-primary-soft ${glassFocus}`}
					>
						<Icon
							name={IconName.ChevronRight}
							className={`h-3 w-3 transition-transform ${detailsExpanded ? 'rotate-90' : ''}`}
						/>
						{detailsExpanded ? 'Hide reasoning and SQL' : 'Show reasoning and SQL'}
					</button>
				)}

				<div id={detailsId} hidden={!detailsExpanded} className={collapsible ? 'mt-3' : ''}>
					{showText && (
						<FormattedContent
							content={message.content}
							className="text-sm leading-relaxed"
						/>
					)}

					{showSql && (
						<SqlBlock
							sql={message.sql!}
							label={
								message.sql!.trim().toUpperCase().startsWith('PREDICT')
									? 'PQL'
									: 'SQL'
							}
							className={showText ? 'mt-3' : undefined}
						/>
					)}
				</div>

				{showQueryResults && (
					<QueryResultsSection
						sqlResponse={message.sqlResponse}
						parsedTable={parsedTable}
						singleCell={singleCellResult}
						className={(showText || showSql) && detailsExpanded ? 'mt-3' : undefined}
					/>
				)}

				<time className="mt-1.5 block text-right text-[10px] opacity-50">
					{formatDate(message.timestamp, 'HH:mm')}
				</time>
			</div>
		</div>
	);
};
