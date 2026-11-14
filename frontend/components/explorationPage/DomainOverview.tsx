// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useState, type ReactNode } from 'react';
import type { SemanticDomain, SemanticDomainLink } from '@/types/exploration';
import { Button } from '@/common/Button';
import { Icon, IconName } from '@/common/icons';
import { ConfirmModal } from '@/common/modal';
import { ButtonTheme, Size } from '@/enums/button';
import { NODE_TYPE_ACCENT_COLOR } from './graph/nodeTypeColors';

export const domainBubbleSize = (termCount: number): number =>
	Math.max(72, Math.min(220, 40 + 12 * Math.sqrt(termCount)));

export const domainLinkWidth = (weight: number, maxWeight: number): number =>
	maxWeight <= 1
		? 1
		: Math.max(1, Math.min(8, 1 + (Math.min(weight, maxWeight) - 1) * (7 / (maxWeight - 1))));

export const formatLinkLabel = (weight: number, sharedTableCount: number): string =>
	`${weight} links · ${sharedTableCount} tables`;

export const formatCertifiedText = (certifiedCount: number, totalTerms: number): string =>
	`${certifiedCount} of ${totalTerms} certified`;

export const calculateCertifiedPercentage = (certifiedCount: number, totalTerms: number): number =>
	totalTerms <= 0 ? 0 : Math.round((certifiedCount / totalTerms) * 100);

export const getTopNeighbours = (
	domainId: string,
	domains: SemanticDomain[],
	links: SemanticDomainLink[],
	limit = 3,
): Array<{ domain: SemanticDomain; weight: number }> => {
	const neighborsMap = new Map<string, number>();
	for (const link of links) {
		let neighborId: string | null = null;
		if (link.source === domainId) neighborId = link.target;
		else if (link.target === domainId) neighborId = link.source;

		if (neighborId && neighborId !== domainId) {
			const currentWeight = neighborsMap.get(neighborId) ?? 0;
			neighborsMap.set(neighborId, Math.max(currentWeight, link.weight));
		}
	}

	const domainMap = new Map(domains.map((d) => [d.id, d]));
	const result: Array<{ domain: SemanticDomain; weight: number }> = [];

	for (const [nId, weight] of neighborsMap.entries()) {
		const neighborDomain = domainMap.get(nId);
		if (neighborDomain) {
			result.push({ domain: neighborDomain, weight });
		}
	}

	result.sort((a, b) => b.weight - a.weight);
	return result.slice(0, limit);
};

type DomainOverviewProps = {
	domains: SemanticDomain[];
	links: SemanticDomainLink[];
	onSelect: (domainId: string) => void;
	onRegenerate?: () => void;
	onRenameDomain?: (domainId: string, name: string, description?: string | null) => Promise<void>;
	isRegenerating?: boolean;
	canEdit?: boolean;
	children?: ReactNode;
};

export const DomainOverview = ({
	domains,
	links,
	onSelect,
	onRegenerate,
	onRenameDomain,
	isRegenerating = false,
	canEdit = true,
	children,
}: DomainOverviewProps) => {
	const [activeDomainId, setActiveDomainId] = useState<string | null>(domains[0]?.id ?? null);
	const [hoveredDomainId, setHoveredDomainId] = useState<string | null>(null);
	const [hoveredLink, setHoveredLink] = useState<SemanticDomainLink | null>(null);
	const [hoveredLinkPos, setHoveredLinkPos] = useState<{ x: number; y: number } | null>(null);

	const [confirmRegenerateOpen, setConfirmRegenerateOpen] = useState(false);
	const [isEditing, setIsEditing] = useState(false);
	const [editName, setEditName] = useState('');
	const [editDescription, setEditDescription] = useState('');
	const [renameError, setRenameError] = useState<string | null>(null);
	const [isSavingRename, setIsSavingRename] = useState(false);

	const activeDomain =
		domains.find((d) => d.id === (hoveredDomainId || activeDomainId)) ?? domains[0] ?? null;

	const positions = domains.map((_, index) => {
		const angle = (2 * Math.PI * index) / Math.max(domains.length, 1) - Math.PI / 2;
		return { x: 400 + 260 * Math.cos(angle), y: 300 + 190 * Math.sin(angle) };
	});
	const positionById = new Map(domains.map((domain, index) => [domain.id, positions[index]]));
	const domainById = new Map(domains.map((domain) => [domain.id, domain]));
	const maxWeight = Math.max(1, ...links.map((link) => link.weight));
	const isCrowded = links.length > 12;

	const handleStartRename = () => {
		if (!activeDomain) return;
		setEditName(activeDomain.name);
		setEditDescription(activeDomain.description ?? '');
		setRenameError(null);
		setIsEditing(true);
	};

	const handleSaveRename = async () => {
		if (!activeDomain || !onRenameDomain) return;
		if (!editName.trim()) {
			setRenameError('Domain name cannot be empty');
			return;
		}
		setIsSavingRename(true);
		setRenameError(null);
		try {
			await onRenameDomain(activeDomain.id, editName.trim(), editDescription.trim() || null);
			setIsEditing(false);
		} catch (err) {
			setRenameError(err instanceof Error ? err.message : 'Failed to rename domain');
		} finally {
			setIsSavingRename(false);
		}
	};

	const topNeighbours = activeDomain ? getTopNeighbours(activeDomain.id, domains, links, 3) : [];

	return (
		<div className="relative flex h-full min-h-[520px] w-full flex-col lg:flex-row">
			<div className="relative flex-1 min-h-[500px] overflow-hidden p-4">
				{/* Top bar controls */}
				<div className="absolute top-4 left-4 right-4 z-20 flex items-center justify-between pointer-events-none">
					<div className="pointer-events-auto flex items-center gap-2">
						{canEdit && onRegenerate && (
							<Button
								theme={ButtonTheme.Secondary}
								size={Size.SMALL}
								type="button"
								disabled={isRegenerating}
								onClick={() => setConfirmRegenerateOpen(true)}
							>
								{isRegenerating ? 'Regenerating...' : 'Regenerate domains'}
							</Button>
						)}
					</div>
					<div className="pointer-events-auto flex items-center gap-2">{children}</div>
				</div>

				<svg
					className="absolute inset-0 h-full w-full"
					viewBox="0 0 800 600"
					preserveAspectRatio="xMidYMid meet"
					aria-hidden="true"
				>
					{links.map((link) => {
						const source = positionById.get(link.source);
						const target = positionById.get(link.target);
						if (!source || !target) return null;
						const midX = (source.x + target.x) / 2;
						const midY = (source.y + target.y) / 2;
						const linkKey = `${link.source}:${link.target}`;
						const isHovered = hoveredLink === link;
						const showLabel =
							!isCrowded ||
							isHovered ||
							hoveredDomainId === link.source ||
							hoveredDomainId === link.target;

						return (
							<g key={linkKey} className="group cursor-pointer">
								<line
									x1={source.x}
									y1={source.y}
									x2={target.x}
									y2={target.y}
									stroke={NODE_TYPE_ACCENT_COLOR.term}
									strokeOpacity={isHovered ? '0.9' : '0.55'}
									strokeWidth={domainLinkWidth(link.weight, maxWeight)}
									onMouseEnter={(e) => {
										setHoveredLink(link);
										const rect = e.currentTarget.getBoundingClientRect();
										setHoveredLinkPos({
											x: rect.left + rect.width / 2,
											y: rect.top,
										});
									}}
									onMouseLeave={() => {
										setHoveredLink(null);
										setHoveredLinkPos(null);
									}}
								/>
								{showLabel && (
									<g transform={`translate(${midX}, ${midY})`}>
										<rect
											x="-55"
											y="-11"
											width="110"
											height="22"
											rx="5"
											fill="var(--background, #ffffff)"
											fillOpacity="0.85"
											stroke={NODE_TYPE_ACCENT_COLOR.term}
											strokeWidth="1"
										/>
										<text
											x="0"
											y="3"
											textAnchor="middle"
											className="text-[10px] font-medium fill-zinc-700 dark:fill-zinc-200"
										>
											{formatLinkLabel(
												link.weight,
												link.shared_table_count ?? 0,
											)}
										</text>
									</g>
								)}
							</g>
						);
					})}
				</svg>

				{domains.map((domain, index) => {
					const position = positions[index];
					const size = domainBubbleSize(domain.term_count);
					const isSelected = activeDomain?.id === domain.id;
					const isHuman = domain.origin === 'human';

					return (
						<button
							key={domain.id}
							type="button"
							aria-label={`Open ${domain.name}, ${domain.term_count} terms`}
							onClick={() => {
								setActiveDomainId(domain.id);
							}}
							onDoubleClick={() => onSelect(domain.id)}
							onMouseEnter={() => setHoveredDomainId(domain.id)}
							onMouseLeave={() => setHoveredDomainId(null)}
							onFocus={() => {
								setHoveredDomainId(domain.id);
								setActiveDomainId(domain.id);
							}}
							onBlur={() => setHoveredDomainId(null)}
							className={`absolute z-10 flex -translate-x-1/2 -translate-y-1/2 flex-col items-center justify-center rounded-full border-2 px-3 text-center shadow-md transition hover:scale-105 focus-visible:outline-2 focus-visible:outline-offset-4 glass-strong ${
								isSelected ? 'ring-2 ring-primary ring-offset-2' : ''
							}`}
							style={{
								left: `${(position.x / 800) * 100}%`,
								top: `${(position.y / 600) * 100}%`,
								width: `${size}px`,
								height: `${size}px`,
								borderColor: NODE_TYPE_ACCENT_COLOR.term,
							}}
						>
							<span className="line-clamp-2 text-sm font-semibold text-heading">
								{domain.name}
							</span>
							<span className="mt-0.5 text-xs text-secondary">
								{domain.term_count} Terms
							</span>
							{isHuman && (
								<span className="mt-0.5 rounded bg-amber-100 px-1.5 py-0.2 text-[10px] font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
									edited
								</span>
							)}
						</button>
					);
				})}

				{/* Hovered link detail popover */}
				{hoveredLink && hoveredLinkPos && (
					<div
						className="pointer-events-none fixed z-30 -translate-x-1/2 -translate-y-full rounded-lg border border-white/80 bg-white/95 p-3 shadow-lg dark:border-white/10 dark:bg-zinc-900/95 text-xs glass-strong"
						style={{ left: `${hoveredLinkPos.x}px`, top: `${hoveredLinkPos.y - 8}px` }}
					>
						<p className="font-semibold text-heading">
							{domainById.get(hoveredLink.source)?.name} ↔{' '}
							{domainById.get(hoveredLink.target)?.name}
						</p>
						<p className="mt-1 text-secondary">
							{hoveredLink.weight} links · {hoveredLink.shared_table_count ?? 0}{' '}
							shared tables
						</p>
						{hoveredLink.relationship_types &&
							hoveredLink.relationship_types.length > 0 && (
								<p className="mt-0.5 text-[11px] text-zinc-500">
									Types: {hoveredLink.relationship_types.join(', ')}
								</p>
							)}
					</div>
				)}
			</div>

			{/* Side Panel */}
			{activeDomain && (
				<aside
					aria-label="Domain details"
					className="z-20 w-full lg:w-80 shrink-0 border-t lg:border-t-0 lg:border-l border-white/70 dark:border-white/10 p-5 flex flex-col gap-4 glass-strong overflow-y-auto"
				>
					<div className="flex items-start justify-between gap-2">
						<div>
							<div className="flex items-center gap-2">
								<span className="text-xs font-bold uppercase text-secondary tracking-wider">
									Business Domain
								</span>
								{activeDomain.origin === 'human' && (
									<span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
										edited
									</span>
								)}
							</div>
							{!isEditing ? (
								<h2 className="text-xl font-bold text-heading mt-1 flex items-center gap-2">
									{activeDomain.name}
									{canEdit && onRenameDomain && (
										<button
											type="button"
											onClick={handleStartRename}
											className="p-1 text-secondary hover:text-primary transition-colors"
											title="Rename domain"
											aria-label="Rename domain"
										>
											<Icon name={IconName.Pencil} className="h-4 w-4" />
										</button>
									)}
								</h2>
							) : null}
						</div>
					</div>

					{/* Inline rename form */}
					{isEditing ? (
						<div className="flex flex-col gap-2 rounded-xl border border-primary/30 p-3 bg-white/50 dark:bg-zinc-900/50">
							<label className="text-xs font-semibold text-heading">
								Domain Name
							</label>
							<input
								type="text"
								value={editName}
								onChange={(e) => setEditName(e.target.value)}
								className="rounded-lg border px-2.5 py-1.5 text-sm bg-white dark:bg-zinc-800 border-zinc-300 dark:border-zinc-700"
							/>
							<label className="text-xs font-semibold text-heading mt-1">
								Description
							</label>
							<textarea
								value={editDescription}
								onChange={(e) => setEditDescription(e.target.value)}
								rows={2}
								className="rounded-lg border px-2.5 py-1.5 text-xs bg-white dark:bg-zinc-800 border-zinc-300 dark:border-zinc-700"
							/>
							{renameError && (
								<p className="text-xs text-red-600 dark:text-red-400">
									{renameError}
								</p>
							)}
							<div className="flex items-center justify-end gap-2 mt-2">
								<Button
									theme={ButtonTheme.Secondary}
									size={Size.SMALL}
									type="button"
									disabled={isSavingRename}
									onClick={() => setIsEditing(false)}
								>
									Cancel
								</Button>
								<Button
									theme={ButtonTheme.Primary}
									size={Size.SMALL}
									type="button"
									disabled={isSavingRename}
									onClick={() => void handleSaveRename()}
								>
									{isSavingRename ? 'Saving...' : 'Save'}
								</Button>
							</div>
						</div>
					) : (
						<p className="text-sm text-body dark:text-zinc-300 leading-relaxed">
							{activeDomain.description || 'No description provided.'}
						</p>
					)}

					<div className="grid grid-cols-2 gap-2 text-xs">
						<div className="rounded-lg bg-white/70 dark:bg-white/[0.06] p-2.5">
							<dt className="text-secondary">Terms</dt>
							<dd className="text-base font-bold text-heading mt-0.5">
								{activeDomain.term_count}
							</dd>
						</div>
						<div className="rounded-lg bg-white/70 dark:bg-white/[0.06] p-2.5">
							<dt className="text-secondary">Certified</dt>
							<dd className="text-xs font-semibold text-heading mt-0.5">
								{formatCertifiedText(
									activeDomain.certified_term_count ?? 0,
									activeDomain.term_count,
								)}
							</dd>
						</div>
						<div className="rounded-lg bg-white/70 dark:bg-white/[0.06] p-2.5">
							<dt className="text-secondary">Linked Tables</dt>
							<dd className="text-base font-bold text-heading mt-0.5">
								{activeDomain.table_count ?? 0}
							</dd>
						</div>
						<div className="rounded-lg bg-white/70 dark:bg-white/[0.06] p-2.5">
							<dt className="text-secondary">Internal Links</dt>
							<dd className="text-base font-bold text-heading mt-0.5">
								{activeDomain.internal_link_count ?? 0}
							</dd>
						</div>
					</div>

					<div className="text-xs text-secondary">
						Attributes:{' '}
						<span className="font-medium text-heading">
							{activeDomain.column_attribute_count ?? 0} column
						</span>{' '}
						·{' '}
						<span className="font-medium text-heading">
							{activeDomain.sql_attribute_count ?? 0} SQL
						</span>
					</div>

					{/* Top terms */}
					{activeDomain.top_terms && activeDomain.top_terms.length > 0 && (
						<div className="flex flex-col gap-1.5">
							<h3 className="text-xs font-bold uppercase text-secondary">
								Top Terms
							</h3>
							<ul className="flex flex-col gap-1">
								{activeDomain.top_terms.slice(0, 3).map((term) => (
									<li
										key={term.id}
										className="rounded-md bg-white/60 dark:bg-white/[0.04] px-2.5 py-1.5 text-xs text-heading truncate"
									>
										{term.name}
									</li>
								))}
							</ul>
						</div>
					)}

					{/* Connected domains */}
					{topNeighbours.length > 0 && (
						<div className="flex flex-col gap-1.5">
							<h3 className="text-xs font-bold uppercase text-secondary">
								Strongest Connected
							</h3>
							<ul className="flex flex-col gap-1 text-xs">
								{topNeighbours.map(({ domain, weight }) => (
									<li
										key={domain.id}
										className="flex items-center justify-between rounded-md bg-white/60 dark:bg-white/[0.04] px-2.5 py-1.5 text-heading"
									>
										<span className="truncate">{domain.name}</span>
										<span className="font-semibold text-secondary ml-2 shrink-0">
											{weight} links
										</span>
									</li>
								))}
							</ul>
						</div>
					)}

					<div className="mt-auto pt-2">
						<Button
							theme={ButtonTheme.Primary}
							size={Size.REGULAR}
							type="button"
							full
							onClick={() => onSelect(activeDomain.id)}
						>
							Inspect domain terms
						</Button>
					</div>
				</aside>
			)}

			<ConfirmModal
				open={confirmRegenerateOpen}
				title="Regenerate domains?"
				message="This will regenerate auto domains for this database. Edited domains are kept."
				confirmLabel="Regenerate"
				cancelLabel="Cancel"
				tone="default"
				onCancel={() => setConfirmRegenerateOpen(false)}
				onConfirm={() => {
					setConfirmRegenerateOpen(false);
					if (onRegenerate) onRegenerate();
				}}
			/>
		</div>
	);
};
