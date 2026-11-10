// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

'use client';

import type { ReactNode } from 'react';

import type { TableRangeMeta } from '@/types/table';

type PaginationProps = {
	/** 1-based current page. */
	page: number;
	pageSize: number;
	totalItems: number;
	onPageChange: (page: number) => void;
	rangeTextFormatFn: (meta: TableRangeMeta) => ReactNode;
	className?: string;
};

const BUTTON =
	'inline-flex h-8 min-w-8 items-center justify-center rounded-full px-1 text-sm font-medium text-zinc-700 transition-colors hover:bg-zinc-100 disabled:cursor-not-allowed disabled:opacity-30 disabled:hover:bg-transparent dark:text-zinc-300 dark:hover:bg-zinc-800 dark:disabled:hover:bg-transparent';
const ACTIVE = 'bg-primary/15 text-primary hover:bg-primary/15 dark:text-primary';

// Page numbers to show: always the first and last page, a window around the
// current one, and 'gap' where numbers are skipped.
const pagesToShow = (page: number, pageCount: number): (number | 'gap')[] => {
	const wanted = new Set([1, pageCount, page - 1, page, page + 1]);
	const sorted = [...wanted].filter((n) => n >= 1 && n <= pageCount).sort((a, b) => a - b);
	const out: (number | 'gap')[] = [];
	sorted.forEach((n, i) => {
		if (i > 0 && n - sorted[i - 1] > 1) out.push('gap');
		out.push(n);
	});
	return out;
};

export const Pagination = ({
	page,
	pageSize,
	totalItems,
	onPageChange,
	rangeTextFormatFn,
	className,
}: PaginationProps) => {
	const pageCount = Math.max(1, Math.ceil(totalItems / pageSize));
	const firstItemIndex = totalItems === 0 ? 0 : (page - 1) * pageSize + 1;
	const lastItemIndex = Math.min(page * pageSize, totalItems);

	return (
		<nav
			aria-label="Pagination"
			className={`flex items-center gap-3 text-sm text-zinc-500 dark:text-zinc-400 ${className ?? ''}`}
		>
			<div className="flex items-center gap-1">
				<button
					type="button"
					aria-label="Previous page"
					className={BUTTON}
					disabled={page <= 1}
					onClick={() => onPageChange(page - 1)}
				>
					‹
				</button>
				{pagesToShow(page, pageCount).map((n, i) =>
					n === 'gap' ? (
						<span key={`gap-${i}`} aria-hidden className="px-1">
							…
						</span>
					) : (
						<button
							key={n}
							type="button"
							aria-label={`Page ${n}`}
							aria-current={n === page ? 'page' : undefined}
							className={`${BUTTON} ${n === page ? ACTIVE : ''}`}
							onClick={() => onPageChange(n)}
						>
							{n}
						</button>
					),
				)}
				<button
					type="button"
					aria-label="Next page"
					className={BUTTON}
					disabled={page >= pageCount}
					onClick={() => onPageChange(page + 1)}
				>
					›
				</button>
			</div>
			<span className="whitespace-nowrap">
				{rangeTextFormatFn({ firstItemIndex, lastItemIndex, totalItems })}
			</span>
		</nav>
	);
};
