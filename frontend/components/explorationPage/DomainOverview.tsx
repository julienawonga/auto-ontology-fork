// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import {
	useEffect,
	useRef,
	useState,
	type PointerEvent as ReactPointerEvent,
	type ReactNode,
} from 'react';
import {
	forceCollide,
	forceLink,
	forceManyBody,
	forceSimulation,
	forceX,
	forceY,
	type Simulation,
	type SimulationLinkDatum,
	type SimulationNodeDatum,
} from 'd3-force';
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

/** Bubbles are drawn larger than `domainBubbleSize` so their names stay readable. */
export const BUBBLE_SCALE = 1.45;

/** How far apart two linked bubbles settle: the stronger the link, the closer. */
export const domainLinkDistance = (weight: number, maxWeight: number): number => {
	const share = maxWeight <= 0 ? 0 : Math.min(Math.max(weight, 0) / maxWeight, 1);
	return Math.round(440 - 200 * share);
};

type Point = { x: number; y: number };

/**
 * Starting layout, as fractions (0 to 1) of the canvas. Two domains sit side by
 * side, more of them on an ellipse; the force layout then takes over from here.
 */
export const initialDomainPositions = (domainIds: string[]): Record<string, Point> => {
	const count = Math.max(domainIds.length, 1);
	const offset = count === 2 ? Math.PI : -Math.PI / 2;
	const positions: Record<string, Point> = {};
	domainIds.forEach((id, index) => {
		const angle = (2 * Math.PI * index) / count + offset;
		positions[id] = { x: 0.5 + 0.3 * Math.cos(angle), y: 0.5 + 0.32 * Math.sin(angle) };
	});
	return positions;
};

export const clampFraction = (value: number): number => Math.max(0.08, Math.min(0.92, value));

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

type SimNode = SimulationNodeDatum & { id: string; r: number };
type SimLink = SimulationLinkDatum<SimNode> & { weight: number };

const prefersReducedMotion = (): boolean =>
	typeof window !== 'undefined' &&
	typeof window.matchMedia === 'function' &&
	window.matchMedia('(prefers-reduced-motion: reduce)').matches;

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
	const [panelOpen, setPanelOpen] = useState(true);

	const [confirmRegenerateOpen, setConfirmRegenerateOpen] = useState(false);
	const [isEditing, setIsEditing] = useState(false);
	const [editName, setEditName] = useState('');
	const [editDescription, setEditDescription] = useState('');
	const [renameError, setRenameError] = useState<string | null>(null);
	const [isSavingRename, setIsSavingRename] = useState(false);

	const activeDomain =
		domains.find((d) => d.id === (hoveredDomainId || activeDomainId)) ?? domains[0] ?? null;

	const domainById = new Map(domains.map((domain) => [domain.id, domain]));
	const maxWeight = Math.max(1, ...links.map((link) => link.weight));
	const isCrowded = links.length > 12;

	// --- Animated layout -------------------------------------------------------
	// A small force simulation (the same library as the Term graph): bubbles push
	// apart, links pull linked bubbles together in proportion to their weight, and
	// dragging one bubble makes its neighbours follow.
	const canvasRef = useRef<HTMLDivElement | null>(null);
	const simulationRef = useRef<Simulation<SimNode, SimLink> | null>(null);
	const nodesRef = useRef<SimNode[]>([]);
	const [size, setSize] = useState({ width: 0, height: 0 });
	const [positions, setPositions] = useState<Record<string, Point>>({});
	const dragRef = useRef<{
		id: string;
		startX: number;
		startY: number;
		offsetX: number;
		offsetY: number;
		moved: boolean;
	} | null>(null);
	const justDraggedRef = useRef(false);

	useEffect(() => {
		const element = canvasRef.current;
		if (!element) return undefined;
		const measure = () => {
			const rect = element.getBoundingClientRect();
			setSize((previous) =>
				Math.abs(previous.width - rect.width) < 40 &&
				Math.abs(previous.height - rect.height) < 40
					? previous
					: { width: rect.width, height: rect.height },
			);
		};
		measure();
		const observer = new ResizeObserver(measure);
		observer.observe(element);
		return () => observer.disconnect();
	}, []);

	const layoutKey = [
		...domains.map((domain) => `${domain.id}:${domain.term_count}`),
		...links.map((link) => `${link.source}>${link.target}:${link.weight}`),
	].join('|');

	useEffect(() => {
		const { width, height } = size;
		if (width <= 0 || height <= 0) return undefined;
		const previous = new Map(nodesRef.current.map((node) => [node.id, node]));
		const seed = initialDomainPositions(domains.map((domain) => domain.id));
		const nodes: SimNode[] = domains.map((domain) => {
			const old = previous.get(domain.id);
			return {
				id: domain.id,
				r: Math.round(domainBubbleSize(domain.term_count) * BUBBLE_SCALE) / 2,
				x: old?.x ?? seed[domain.id].x * width,
				y: old?.y ?? seed[domain.id].y * height,
			};
		});
		nodesRef.current = nodes;
		const known = new Set(nodes.map((node) => node.id));
		const simLinks: SimLink[] = links
			.filter((link) => known.has(link.source) && known.has(link.target))
			.map((link) => ({ source: link.source, target: link.target, weight: link.weight }));

		const publish = () => {
			for (const node of nodes) {
				const margin = node.r + 8;
				node.x = Math.max(margin, Math.min(width - margin, node.x ?? width / 2));
				node.y = Math.max(margin, Math.min(height - margin, node.y ?? height / 2));
			}
			setPositions(
				Object.fromEntries(nodes.map((node) => [node.id, { x: node.x!, y: node.y! }])),
			);
		};

		const simulation = forceSimulation<SimNode>(nodes)
			.force(
				'link',
				forceLink<SimNode, SimLink>(simLinks)
					.id((node) => node.id)
					.distance((link) => domainLinkDistance(link.weight, maxWeight))
					.strength((link) => 0.06 + 0.3 * (link.weight / maxWeight)),
			)
			.force('charge', forceManyBody<SimNode>().strength(-1400))
			.force('collide', forceCollide<SimNode>((node) => node.r + 28).iterations(2))
			.force('x', forceX<SimNode>(width / 2).strength(0.05))
			.force('y', forceY<SimNode>(height / 2).strength(0.05))
			.velocityDecay(0.5)
			.on('tick', publish);
		simulationRef.current = simulation;

		if (prefersReducedMotion()) {
			simulation.stop();
			for (let i = 0; i < 300; i += 1) simulation.tick();
			publish();
		} else {
			simulation.alpha(0.9).restart();
		}
		return () => {
			simulation.stop();
			simulationRef.current = null;
		};
		// The layout restarts only when the domains, their links or the canvas size change.
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [layoutKey, size.width, size.height]);

	const positionOf = (id: string): Point => {
		const known = positions[id];
		if (known) return known;
		const seed = initialDomainPositions(domains.map((domain) => domain.id))[id];
		return { x: (seed?.x ?? 0.5) * size.width, y: (seed?.y ?? 0.5) * size.height };
	};
	const positionById = new Map(domains.map((domain) => [domain.id, positionOf(domain.id)]));

	const pointerInCanvas = (event: ReactPointerEvent<HTMLElement>): Point | null => {
		const rect = canvasRef.current?.getBoundingClientRect();
		return rect ? { x: event.clientX - rect.left, y: event.clientY - rect.top } : null;
	};

	const handleBubblePointerDown = (event: ReactPointerEvent<HTMLButtonElement>, id: string) => {
		const pointer = pointerInCanvas(event);
		const node = nodesRef.current.find((item) => item.id === id);
		if (!pointer || !node) return;
		event.currentTarget.setPointerCapture(event.pointerId);
		dragRef.current = {
			id,
			startX: event.clientX,
			startY: event.clientY,
			offsetX: pointer.x - (node.x ?? 0),
			offsetY: pointer.y - (node.y ?? 0),
			moved: false,
		};
	};

	const handleBubblePointerMove = (event: ReactPointerEvent<HTMLButtonElement>) => {
		const drag = dragRef.current;
		const pointer = pointerInCanvas(event);
		const node = drag ? nodesRef.current.find((item) => item.id === drag.id) : null;
		if (!drag || !pointer || !node) return;
		// Below a few pixels it is a click, not a drag.
		if (
			!drag.moved &&
			Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 5
		) {
			return;
		}
		drag.moved = true;
		const margin = node.r + 8;
		node.fx = Math.max(margin, Math.min(size.width - margin, pointer.x - drag.offsetX));
		node.fy = Math.max(margin, Math.min(size.height - margin, pointer.y - drag.offsetY));
		// Keep the simulation warm while dragging so the neighbours follow.
		simulationRef.current?.alphaTarget(0.3).restart();
	};

	const handleBubblePointerUp = (event: ReactPointerEvent<HTMLButtonElement>) => {
		if (event.currentTarget.hasPointerCapture(event.pointerId)) {
			event.currentTarget.releasePointerCapture(event.pointerId);
		}
		const drag = dragRef.current;
		const node = drag ? nodesRef.current.find((item) => item.id === drag.id) : null;
		if (node) {
			node.fx = null;
			node.fy = null;
		}
		simulationRef.current?.alphaTarget(0);
		// The click that follows a drag must not select or open anything.
		justDraggedRef.current = drag?.moved ?? false;
		dragRef.current = null;
		if (justDraggedRef.current) {
			window.setTimeout(() => {
				justDraggedRef.current = false;
			}, 0);
		}
	};

	// --- Rename ----------------------------------------------------------------
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
	const certifiedPercentage = activeDomain
		? calculateCertifiedPercentage(
				activeDomain.certified_term_count ?? 0,
				activeDomain.term_count,
			)
		: 0;

	return (
		<div className="relative flex h-full min-h-[520px] w-full flex-col lg:flex-row">
			<div
				ref={canvasRef}
				className="relative flex-1 min-h-[500px] overflow-hidden p-4 select-none"
			>
				{/* Bottom-left: kept clear of the page's own controls at the top and right */}
				<div className="absolute bottom-4 left-4 z-20 flex items-center gap-3">
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
					{children}
				</div>

				{/* Re-open the details panel after it was folded away */}
				{!panelOpen && activeDomain && (
					<button
						type="button"
						onClick={() => setPanelOpen(true)}
						aria-label="Show domain details"
						aria-expanded={false}
						className="absolute right-4 top-[84px] z-20 flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium text-heading shadow-md glass-strong hover:text-primary"
					>
						<Icon name={IconName.Sidebar} className="h-4 w-4" />
						Details
					</button>
				)}

				{/* Links, in the same pixel space as the bubbles */}
				<svg className="absolute inset-0 h-full w-full" aria-hidden="true">
					{links.map((link) => {
						const source = positionById.get(link.source);
						const target = positionById.get(link.target);
						if (!source || !target) return null;
						const isHovered = hoveredLink === link;
						const coordinates = {
							x1: source.x,
							y1: source.y,
							x2: target.x,
							y2: target.y,
						};
						const showHover = (e: React.MouseEvent<SVGLineElement>) => {
							setHoveredLink(link);
							setHoveredLinkPos({ x: e.clientX, y: e.clientY });
						};
						return (
							<g key={`${link.source}:${link.target}`}>
								<line
									{...coordinates}
									stroke={NODE_TYPE_ACCENT_COLOR.term}
									strokeOpacity={isHovered ? '0.9' : '0.55'}
									strokeWidth={domainLinkWidth(link.weight, maxWeight)}
									strokeLinecap="round"
								/>
								{/* Wide invisible line: an easy target for the hover detail */}
								<line
									{...coordinates}
									stroke="transparent"
									strokeWidth={Math.max(
										16,
										domainLinkWidth(link.weight, maxWeight) + 8,
									)}
									className="cursor-pointer"
									onMouseEnter={showHover}
									onMouseMove={showHover}
									onMouseLeave={() => {
										setHoveredLink(null);
										setHoveredLinkPos(null);
									}}
								/>
							</g>
						);
					})}
				</svg>

				{/* Link labels, centred on the middle of each link */}
				{links.map((link) => {
					const source = positionById.get(link.source);
					const target = positionById.get(link.target);
					if (!source || !target) return null;
					const showLabel =
						!isCrowded ||
						hoveredLink === link ||
						hoveredDomainId === link.source ||
						hoveredDomainId === link.target;
					if (!showLabel) return null;
					return (
						<div
							key={`label:${link.source}:${link.target}`}
							className="pointer-events-none absolute z-[5] -translate-x-1/2 -translate-y-1/2 whitespace-nowrap rounded-md border bg-white/90 px-2 py-0.5 text-[11px] font-medium text-zinc-700 shadow-sm dark:bg-zinc-900/90 dark:text-zinc-200"
							style={{
								left: `${(source.x + target.x) / 2}px`,
								top: `${(source.y + target.y) / 2}px`,
								borderColor: NODE_TYPE_ACCENT_COLOR.term,
							}}
						>
							{formatLinkLabel(link.weight, link.shared_table_count ?? 0)}
						</div>
					);
				})}

				{size.width > 0 &&
					domains.map((domain) => {
						const position = positionOf(domain.id);
						const bubble = Math.round(
							domainBubbleSize(domain.term_count) * BUBBLE_SCALE,
						);
						const isSelected = activeDomain?.id === domain.id;
						const isHuman = domain.origin === 'human';

						return (
							<button
								key={domain.id}
								type="button"
								aria-label={`Open ${domain.name}, ${domain.term_count} terms`}
								title="Drag to move, double-click to open its Terms"
								onPointerDown={(event) => handleBubblePointerDown(event, domain.id)}
								onPointerMove={handleBubblePointerMove}
								onPointerUp={handleBubblePointerUp}
								onPointerCancel={handleBubblePointerUp}
								onClick={() => {
									if (justDraggedRef.current) return;
									setActiveDomainId(domain.id);
									setPanelOpen(true);
								}}
								onDoubleClick={() => {
									if (justDraggedRef.current) return;
									onSelect(domain.id);
								}}
								onMouseEnter={() => setHoveredDomainId(domain.id)}
								onMouseLeave={() => setHoveredDomainId(null)}
								onFocus={() => {
									setHoveredDomainId(domain.id);
									setActiveDomainId(domain.id);
								}}
								onBlur={() => setHoveredDomainId(null)}
								className={`absolute z-10 flex -translate-x-1/2 -translate-y-1/2 cursor-grab touch-none flex-col items-center justify-center rounded-full border-2 px-4 text-center shadow-md transition-shadow active:cursor-grabbing focus-visible:outline-2 focus-visible:outline-offset-4 glass-strong ${
									isSelected ? 'ring-2 ring-primary ring-offset-2' : ''
								}`}
								style={{
									left: `${position.x}px`,
									top: `${position.y}px`,
									width: `${bubble}px`,
									height: `${bubble}px`,
									borderColor: NODE_TYPE_ACCENT_COLOR.term,
								}}
							>
								<span className="line-clamp-3 break-words text-sm font-semibold leading-tight text-heading">
									{domain.name}
								</span>
								<span className="mt-1 text-xs text-secondary">
									{domain.term_count} Terms
								</span>
								{isHuman && (
									<span className="mt-0.5 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
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

			{/* Side panel */}
			{activeDomain && panelOpen && (
				<aside
					aria-label="Domain details"
					className="z-20 w-full lg:w-80 lg:mt-20 shrink-0 border-t lg:border-t-0 lg:border-l border-white/70 dark:border-white/10 pb-32 flex flex-col gap-4 glass-strong overflow-y-auto"
				>
					<header className="border-b border-white/60 bg-gradient-to-br from-primary/15 via-primary/5 to-transparent p-5 dark:border-white/10">
						<div className="flex items-center justify-between gap-2">
							<div className="flex items-center gap-2">
								<span className="flex h-7 w-7 items-center justify-center rounded-lg bg-primary text-white shadow-sm">
									<Icon name={IconName.Exploration} className="h-4 w-4" />
								</span>
								<span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-primary">
									Business domain
								</span>
							</div>
							<button
								type="button"
								onClick={() => setPanelOpen(false)}
								aria-label="Hide domain details"
								aria-expanded
								title="Hide details"
								className="rounded-md p-1.5 text-secondary transition-colors hover:bg-white/70 hover:text-primary dark:hover:bg-white/10"
							>
								<Icon name={IconName.ChevronRight} className="h-4 w-4" />
							</button>
						</div>

						{!isEditing && (
							<h2 className="mt-3 flex items-start gap-2 text-xl font-bold leading-snug text-heading">
								<span className="min-w-0 break-words">{activeDomain.name}</span>
								{canEdit && onRenameDomain && (
									<button
										type="button"
										onClick={handleStartRename}
										className="mt-0.5 shrink-0 p-1 text-secondary transition-colors hover:text-primary"
										title="Rename domain"
										aria-label="Rename domain"
									>
										<Icon name={IconName.Pencil} className="h-4 w-4" />
									</button>
								)}
							</h2>
						)}

						<div className="mt-2 flex flex-wrap gap-1.5 text-[11px] font-medium">
							<span className="rounded-full bg-white/80 px-2 py-0.5 text-heading dark:bg-white/10">
								{activeDomain.term_count} Terms
							</span>
							{activeDomain.origin === 'human' ? (
								<span className="rounded-full bg-amber-100 px-2 py-0.5 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300">
									Edited by a person
								</span>
							) : (
								<span className="rounded-full bg-white/80 px-2 py-0.5 text-secondary dark:bg-white/10">
									Grouped by the model
								</span>
							)}
						</div>
					</header>

					<div className="flex flex-col gap-4 px-5">
						{/* Inline rename form */}
						{isEditing ? (
							<div className="flex flex-col gap-2 rounded-xl border border-primary/30 bg-white/50 p-3 dark:bg-zinc-900/50">
								<label className="text-xs font-semibold text-heading">
									Domain Name
								</label>
								<input
									type="text"
									value={editName}
									onChange={(e) => setEditName(e.target.value)}
									className="rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-800"
								/>
								<label className="mt-1 text-xs font-semibold text-heading">
									Description
								</label>
								<textarea
									value={editDescription}
									onChange={(e) => setEditDescription(e.target.value)}
									rows={2}
									className="rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-xs dark:border-zinc-700 dark:bg-zinc-800"
								/>
								{renameError && (
									<p className="text-xs text-red-600 dark:text-red-400">
										{renameError}
									</p>
								)}
								<div className="mt-2 flex items-center justify-end gap-2">
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
							<p className="text-sm leading-relaxed text-body dark:text-zinc-300">
								{activeDomain.description || 'No description provided.'}
							</p>
						)}

						<Button
							theme={ButtonTheme.Primary}
							size={Size.REGULAR}
							type="button"
							full
							onClick={() => onSelect(activeDomain.id)}
						>
							{"Open this domain's Terms"}
						</Button>

						<dl className="grid grid-cols-2 gap-2 text-xs">
							<div className="rounded-lg bg-white/70 p-2.5 dark:bg-white/[0.06]">
								<dt className="text-secondary">Terms</dt>
								<dd className="mt-0.5 text-base font-bold text-heading">
									{activeDomain.term_count}
								</dd>
							</div>
							<div className="rounded-lg bg-white/70 p-2.5 dark:bg-white/[0.06]">
								<dt className="text-secondary">Certified</dt>
								<dd className="mt-0.5 text-xs font-semibold text-heading">
									{formatCertifiedText(
										activeDomain.certified_term_count ?? 0,
										activeDomain.term_count,
									)}
								</dd>
								<div className="mt-1.5 h-1 overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-700">
									<div
										className="h-full rounded-full bg-primary"
										style={{ width: `${certifiedPercentage}%` }}
									/>
								</div>
							</div>
							<div className="rounded-lg bg-white/70 p-2.5 dark:bg-white/[0.06]">
								<dt className="text-secondary">Linked Tables</dt>
								<dd className="mt-0.5 text-base font-bold text-heading">
									{activeDomain.table_count ?? 0}
								</dd>
							</div>
							<div className="rounded-lg bg-white/70 p-2.5 dark:bg-white/[0.06]">
								<dt className="text-secondary">Internal Links</dt>
								<dd className="mt-0.5 text-base font-bold text-heading">
									{activeDomain.internal_link_count ?? 0}
								</dd>
							</div>
						</dl>

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
											className="truncate rounded-md bg-white/60 px-2.5 py-1.5 text-xs text-heading dark:bg-white/[0.04]"
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
											className="flex items-center justify-between rounded-md bg-white/60 px-2.5 py-1.5 text-heading dark:bg-white/[0.04]"
										>
											<span className="truncate">{domain.name}</span>
											<span className="ml-2 shrink-0 font-semibold text-secondary">
												{weight} links
											</span>
										</li>
									))}
								</ul>
							</div>
						)}
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
