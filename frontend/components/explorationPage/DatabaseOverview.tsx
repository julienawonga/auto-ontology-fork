// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import type { SemanticDatabaseOverviewItem } from '@/types/exploration';
import { NODE_TYPE_ACCENT_COLOR } from './graph/nodeTypeColors';

export const overviewBubbleSize = (termCount: number): number =>
	Math.max(72, Math.min(220, 40 + 12 * Math.sqrt(termCount)));

type DatabaseOverviewProps = {
	databases: SemanticDatabaseOverviewItem[];
	onSelect: (databaseId: string) => void;
};

export const DatabaseOverview = ({ databases, onSelect }: DatabaseOverviewProps) => {
	const populated = databases
		.filter((database) => database.term_count > 0)
		.sort((left, right) => right.term_count - left.term_count);

	if (populated.length === 0) return null;

	return (
		<div className="flex h-full flex-wrap content-center items-center justify-center gap-5 overflow-auto p-8">
			{populated.map((database) => {
				const countLabel = `${database.term_count} ${database.term_count === 1 ? 'term' : 'terms'}`;
				const size = overviewBubbleSize(database.term_count);
				return (
					<button
						key={database.database_id}
						type="button"
						aria-label={`Open ${database.database_name}, ${countLabel}`}
						onClick={() => onSelect(database.database_id)}
						className="flex shrink-0 flex-col items-center justify-center rounded-full border-2 px-3 text-center shadow-md transition hover:scale-105 focus-visible:outline-2 focus-visible:outline-offset-4 glass-strong"
						style={{
							width: `${size}px`,
							height: `${size}px`,
							borderColor: NODE_TYPE_ACCENT_COLOR.term,
						}}
					>
						<span className="line-clamp-2 text-sm font-semibold text-heading">
							{database.database_name}
						</span>
						<span className="mt-1 text-xs text-secondary">{countLabel}</span>
					</button>
				);
			})}
		</div>
	);
};
