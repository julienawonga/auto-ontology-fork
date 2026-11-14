// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useQuery } from '@tanstack/react-query';
import { termsApi } from '@/api/terms';
import { Button } from '@/common/Button';
import { CertificationBadge } from '@/common/CertificationBadge';
import { Label } from '@/common/Label';
import { OwnerLabel } from '@/common/OwnerLabel';
import { ButtonTheme, Size } from '@/enums/button';
import { findOwner } from '@/lib/owners';
import type { Owner } from '@/types/owners';
import type { Term } from '@/types/terms';

/** How many column attributes the panel lists before pointing to the full page. */
const ATTRIBUTE_PREVIEW_LIMIT = 6;

type TermPreviewPanelProps = {
	term: Term;
	owners: Owner[];
	columnAttributeCount: number;
	sqlAttributeCount: number;
	relatedCount: number;
	onOpen: (term: Term) => void;
};

const sectionTitle = 'text-sm font-semibold text-glass-text dark:text-zinc-50';

/**
 * The selected term at a glance, beside the list: what it means, its first
 * column attributes, and how it connects. Everything else, editing included,
 * stays on the term's full page.
 */
export const TermPreviewPanel = ({
	term,
	owners,
	columnAttributeCount,
	sqlAttributeCount,
	relatedCount,
	onOpen,
}: TermPreviewPanelProps) => {
	const attributes = useQuery({
		queryKey: ['terms', term.id, 'column-attributes', 'preview', ATTRIBUTE_PREVIEW_LIMIT],
		queryFn: async () => {
			const res = await termsApi.getColumnAttributes(term.id, {
				skip: 0,
				limit: ATTRIBUTE_PREVIEW_LIMIT,
			});
			if (res.error) throw new Error(res.message ?? 'Failed to load attributes.');
			return res.data ?? [];
		},
		enabled: columnAttributeCount > 0,
	});
	const tags = term.tags ?? [];
	const owner = findOwner(owners, term.owner_id);

	return (
		<aside
			aria-label="Term details"
			className="glass-strong flex flex-col gap-5 rounded-[24px] border border-white/80 p-6 dark:border-white/10"
		>
			<div className="flex flex-col gap-1.5">
				<div className="flex items-center justify-between gap-2">
					<span className="text-xs font-bold tracking-wider text-secondary uppercase dark:text-zinc-400">
						Term
					</span>
					<CertificationBadge status={term.certification} />
				</div>
				<h2 className="text-[22px] leading-tight font-bold text-glass-text dark:text-zinc-50">
					{term.name}
				</h2>
				{(term.synonyms ?? []).length > 0 && (
					<p className="text-xs text-secondary dark:text-zinc-400">
						Also: {(term.synonyms ?? []).join(', ')}
					</p>
				)}
			</div>

			<p className="text-sm leading-relaxed text-body dark:text-zinc-300">
				{term.description != null && term.description.trim() !== ''
					? term.description
					: 'No description yet.'}
			</p>

			<div className="flex flex-col gap-2">
				<span className={sectionTitle}>
					Column attributes{' '}
					<span className="font-normal text-secondary dark:text-zinc-400">
						· {columnAttributeCount}
					</span>
				</span>
				{columnAttributeCount === 0 ? (
					<p className="text-sm text-secondary dark:text-zinc-400">None yet.</p>
				) : attributes.isPending ? (
					<p className="text-sm text-secondary dark:text-zinc-400" role="status">
						Loading…
					</p>
				) : attributes.isError ? (
					<p className="text-sm text-red-600 dark:text-red-400">
						Couldn&apos;t load the attributes.
					</p>
				) : (
					<ul className="flex flex-col gap-1.5">
						{attributes.data.map((attr) => (
							<li
								key={attr.id}
								className="flex items-center justify-between gap-3 rounded-xl bg-white/70 px-3 py-2.5 text-sm dark:bg-white/[0.06]"
							>
								<span className="min-w-0 truncate text-heading dark:text-zinc-100">
									{attr.name}
								</span>
								{attr.datatype ? (
									<span className="shrink-0 font-mono text-xs text-secondary dark:text-zinc-400">
										{attr.datatype}
									</span>
								) : null}
							</li>
						))}
						{columnAttributeCount > attributes.data.length && (
							<li className="text-xs text-secondary dark:text-zinc-400">
								and {columnAttributeCount - attributes.data.length} more on the full
								page
							</li>
						)}
					</ul>
				)}
			</div>

			<dl className="grid grid-cols-2 gap-3 text-sm">
				<div className="rounded-xl bg-white/70 px-3 py-2.5 dark:bg-white/[0.06]">
					<dt className="text-xs text-secondary dark:text-zinc-400">SQL attributes</dt>
					<dd className="text-lg font-bold text-glass-text tabular-nums dark:text-zinc-50">
						{sqlAttributeCount}
					</dd>
				</div>
				<div className="rounded-xl bg-white/70 px-3 py-2.5 dark:bg-white/[0.06]">
					<dt className="text-xs text-secondary dark:text-zinc-400">Related terms</dt>
					<dd className="text-lg font-bold text-glass-text tabular-nums dark:text-zinc-50">
						{relatedCount}
					</dd>
				</div>
			</dl>

			{(tags.length > 0 || term.zones.length > 0 || owner) && (
				<div className="flex flex-wrap items-center gap-2">
					{tags.map((tag) => (
						<Label key={tag.id} label={tag.name} />
					))}
					{term.zones.map((zone) => (
						<Label
							key={zone.id}
							label={zone.name}
							color={zone.color}
							muted={!zone.enabled}
						/>
					))}
					<OwnerLabel owner={owner} />
				</div>
			)}

			<Button
				theme={ButtonTheme.Primary}
				size={Size.LARGE}
				type="button"
				full
				onClick={() => onOpen(term)}
			>
				Open term
			</Button>
		</aside>
	);
};
