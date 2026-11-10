// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';

import { Button, SelectButton } from '@/common/Button';
import { EmptyState } from '@/common/EmptyState';
import { Size, ButtonTheme, SelectButtonTheme } from '@/enums/button';
import { EmptyStateVariant } from '@/enums/emptyState';
import { Icon, IconName } from '@/common/icons';
import { InfiniteScroll } from '@/common/InfiniteScroll';
import { PopoverMenu } from '@/common/PopoverMenu';
import { SearchInput } from '@/common/SearchInput';
import { SkeletonCard } from '@/common/Skeleton';
import { ConfirmModal, ModalCreateNewItem } from '@/common/modal';
import { SqlBlock, SqlEditor } from '@/common/SqlBlock';
import { Text } from '@/common/Text';
import { TextVariant } from '@/enums/text';
import { analyses } from '@/api/analyses';
import { pqlAnalyses } from '@/api/pqlAnalyses';
import { useDebouncedValue } from '@/hooks/useDebouncedValue';
import { DEFAULT_PAGE_SIZE, useInfiniteList } from '@/hooks/useInfiniteList';

export type AnalysisViewProps = Record<string, never>;

type AnalysisMode = 'sql' | 'pql';

// Normalized item so the SQL and PQL variants share one rendering/edit path.
type AnalysisItem = {
	id: string;
	name: string;
	description: string;
	code: string;
};

const FIELD_INPUT_CLASSNAME =
	'w-full rounded-xl border border-zinc-900/15 bg-white/70 px-3 py-2 text-sm text-body outline-none transition-colors placeholder:text-secondary focus:border-primary focus:ring-2 focus:ring-primary/30 dark:border-white/15 dark:bg-white/[0.06] dark:text-zinc-300 dark:placeholder:text-zinc-500';

const FIELD_LABEL_CLASSNAME = 'mb-1.5 block text-sm font-medium text-heading dark:text-zinc-100';

const MODE_LABEL: Record<AnalysisMode, string> = { sql: 'SQL', pql: 'PQL' };
const SKELETON_CARD_HEIGHT = 184;
const SKELETON_CARD_GAP = 16;
const LOADING_AREA_RESERVED_HEIGHT = 112;
const MAX_LOADING_SKELETON_COUNT = 4;

const getLoadingSkeletonCount = () =>
	Math.max(
		1,
		Math.min(
			MAX_LOADING_SKELETON_COUNT,
			Math.floor(
				(window.innerHeight - LOADING_AREA_RESERVED_HEIGHT + SKELETON_CARD_GAP) /
					(SKELETON_CARD_HEIGHT + SKELETON_CARD_GAP),
			),
		),
	);

export const AnalysisView = () => {
	const searchParams = useSearchParams();
	const focusId = searchParams.get('focus');
	const urlMode: AnalysisMode = searchParams.get('mode') === 'pql' ? 'pql' : 'sql';
	const [tabOverride, setTabOverride] = useState<AnalysisMode | null>(null);
	const [focusSeen, setFocusSeen] = useState<string | null>(null);
	if (focusId !== focusSeen) {
		setFocusSeen(focusId);
		setTabOverride(null);
	}
	const mode: AnalysisMode = tabOverride ?? urlMode;
	const setMode = (next: AnalysisMode) => setTabOverride(next);
	const [searchQuery, setSearchQuery] = useState('');
	const debouncedSearchQuery = useDebouncedValue(searchQuery.trim(), 1000);
	const [hasLoaded, setHasLoaded] = useState(false);
	const [loadingSkeletonCount, setLoadingSkeletonCount] = useState(3);

	const [modalOpen, setModalOpen] = useState(false);
	const [editingId, setEditingId] = useState<string | null>(null);
	const [name, setName] = useState('');
	const [description, setDescription] = useState('');
	const [code, setCode] = useState('');
	const [submitting, setSubmitting] = useState(false);
	const [submitError, setSubmitError] = useState<string | null>(null);
	const [validating, setValidating] = useState(false);
	const [validationMessage, setValidationMessage] = useState<string | null>(null);
	// SQL requires an explicit validation pass before saving; PQL does not
	// (it is validated against the prediction graph at predict time).
	const [sqlValidated, setSqlValidated] = useState(false);

	const [deletingItem, setDeletingItem] = useState<AnalysisItem | null>(null);

	// Cards start folded (title and one line of description); the one a link
	// focuses on opens with its details.
	const [expandedIds, setExpandedIds] = useState<ReadonlySet<string>>(() => new Set());
	const isExpanded = (id: string) => expandedIds.has(id) || id === focusId;
	const toggleExpanded = (id: string) =>
		setExpandedIds((prev) => {
			const next = new Set(prev);
			if (isExpanded(id)) next.delete(id);
			else next.add(id);
			return next;
		});
	const [deleting, setDeleting] = useState(false);
	const [deleteError, setDeleteError] = useState<string | null>(null);

	const isEditing = editingId !== null;
	const isPql = mode === 'pql';

	useEffect(() => {
		const updateLoadingSkeletonCount = () => setLoadingSkeletonCount(getLoadingSkeletonCount());

		updateLoadingSkeletonCount();
		window.addEventListener('resize', updateLoadingSkeletonCount);

		return () => window.removeEventListener('resize', updateLoadingSkeletonCount);
	}, []);

	const fetchAnalysesPage = useCallback(
		async (skip: number, limit: number) => {
			const params = {
				...(debouncedSearchQuery ? { query: debouncedSearchQuery } : {}),
				skip,
				limit,
			};
			if (isPql) {
				const res = await pqlAnalyses.list(params);
				if (res.error) return { error: res.message ?? 'Failed to load PQL analyses' };
				setHasLoaded(true);
				return {
					items: (res.data ?? []).map((a) => ({
						id: a.id,
						name: a.name,
						description: a.description,
						code: a.pql,
					})),
					total: res.total ?? 0,
				};
			}
			const res = await analyses.list(params);
			if (res.error) return { error: res.message ?? 'Failed to load custom analyses' };
			setHasLoaded(true);
			return {
				items: (res.data ?? []).map((a) => ({
					id: a.id,
					name: a.name,
					description: a.description,
					code: a.sql,
				})),
				total: res.total ?? 0,
			};
		},
		[debouncedSearchQuery, isPql],
	);

	const {
		items,
		setItems,
		isLoading: loading,
		isLoadingMore,
		error,
		hasMore,
		loadMore,
		reload,
	} = useInfiniteList(fetchAnalysesPage, {
		pageSize: DEFAULT_PAGE_SIZE,
		itemKey: (item) => item.id,
	});

	useEffect(() => {
		// `error` stops this as it stops `InfiniteScroll`'s sentinel: a failed
		// page leaves `hasMore` true and clears `isLoadingMore`, and `loadMore`
		// clears the error before retrying, so without the guard a server that
		// keeps failing is retried in a loop. The Retry control owns that.
		if (focusId == null || loading || isLoadingMore || !hasMore || error != null) return;
		if (items.some((item) => item.id === focusId)) return;
		loadMore();
	}, [focusId, items, loading, isLoadingMore, hasMore, error, loadMore]);

	useEffect(() => {
		if (focusId == null || loading) return;
		document
			.getElementById(`analysis-${focusId}`)
			?.scrollIntoView({ block: 'center', behavior: 'smooth' });
	}, [focusId, items, loading]);

	const openCreateModal = () => {
		setEditingId(null);
		setName('');
		setDescription('');
		setCode('');
		setSubmitError(null);
		setValidationMessage(null);
		setSqlValidated(false);
		setModalOpen(true);
	};

	const openEditModal = (item: AnalysisItem) => {
		setEditingId(item.id);
		setName(item.name);
		setDescription(item.description);
		setCode(item.code);
		setSubmitError(null);
		setValidationMessage(null);
		setSqlValidated(true);
		setModalOpen(true);
	};

	const handleCodeChange = (value: string) => {
		setCode(value);
		setSqlValidated(false);
		setValidationMessage(null);
	};

	const handleClose = () => {
		if (submitting || validating) return;
		setModalOpen(false);
		setSubmitError(null);
		setValidationMessage(null);
		setSqlValidated(false);
	};

	const openDeleteModal = (item: AnalysisItem) => {
		setDeletingItem(item);
		setDeleteError(null);
	};

	const handleDeleteClose = () => {
		if (deleting) return;
		setDeletingItem(null);
		setDeleteError(null);
	};

	const handleDeleteConfirm = async () => {
		if (deletingItem == null) return;
		setDeleting(true);
		const res = isPql
			? await pqlAnalyses.delete(deletingItem.id)
			: await analyses.delete(deletingItem.id);
		setDeleting(false);
		if (res.error) {
			setDeleteError(res.message ?? `Failed to delete ${MODE_LABEL[mode]} analysis`);
			return;
		}
		// Not a client-side filter: the next page is read from the count of
		// rows fetched so far, and the delete shifted every later row up one,
		// so one analysis would never be read at all.
		reload();
		setDeletingItem(null);
	};

	const trimmedName = name.trim();
	const trimmedDescription = description.trim();
	const trimmedCode = code.trim();

	const canSubmit =
		!submitting &&
		trimmedName.length > 0 &&
		trimmedDescription.length > 0 &&
		trimmedCode.length > 0 &&
		// PQL has no client-side validation gate; SQL must be validated first.
		(isPql || sqlValidated);

	const handleValidateSql = async () => {
		if (trimmedCode.length === 0) return;
		setValidating(true);
		setValidationMessage(null);
		setSqlValidated(false);
		const res = await analyses.validate(trimmedCode);
		setValidating(false);
		if (res.error) {
			setValidationMessage(res.message ?? 'SQL validation failed');
			return;
		}
		const isValid = res.data.valid === true;
		setSqlValidated(isValid);
		setValidationMessage(isValid ? 'SQL is valid.' : 'SQL validation failed');
	};

	const handleSubmit = async () => {
		if (!canSubmit) return;
		setSubmitting(true);
		setSubmitError(null);

		let savedItem: AnalysisItem | null = null;
		let failure: string | null = null;

		if (isPql) {
			const payload = {
				name: trimmedName,
				description: trimmedDescription,
				pql: trimmedCode,
			};
			const res =
				editingId !== null
					? await pqlAnalyses.update(editingId, payload)
					: await pqlAnalyses.create(payload);
			if (res.error) {
				failure = res.message ?? null;
			} else {
				savedItem = {
					id: res.data.id,
					name: res.data.name,
					description: res.data.description,
					code: res.data.pql,
				};
			}
		} else {
			const payload = {
				name: trimmedName,
				description: trimmedDescription,
				sql: trimmedCode,
			};
			const res =
				editingId !== null
					? await analyses.update(editingId, payload)
					: await analyses.create(payload);
			if (res.error) {
				failure = res.message ?? null;
			} else {
				savedItem = {
					id: res.data.id,
					name: res.data.name,
					description: res.data.description,
					code: res.data.sql,
				};
			}
		}

		setSubmitting(false);

		if (savedItem === null) {
			setSubmitError(
				failure ??
					(editingId !== null
						? `Failed to update ${MODE_LABEL[mode]} analysis`
						: `Failed to create ${MODE_LABEL[mode]} analysis`),
			);
			return;
		}

		const saved = savedItem;
		// A create and a rename both move rows in the server's name order,
		// which leaves the next page read from a window that no longer lines
		// up with what is held. An edit that keeps the name cannot reorder
		// anything, so it patches the card in place.
		const renamed = items.some((a) => a.id === editingId && a.name !== saved.name);
		if (editingId === null || renamed) {
			reload();
		} else {
			setItems((prev) => prev.map((a) => (a.id === editingId ? saved : a)));
		}
		setModalOpen(false);
	};

	return (
		<div className="flex h-full w-full flex-col">
			<header className="flex flex-wrap items-end gap-4 px-6 pt-8 pb-2 sm:px-10">
				<div className="flex min-w-0 flex-col gap-1.5">
					<h1 className="text-[28px] leading-tight font-bold tracking-tight text-glass-text dark:text-zinc-50">
						{isPql ? 'PQL Analyses' : 'Custom Analyses'}
					</h1>
					<p className="text-sm text-secondary dark:text-zinc-400">
						Certified queries the chat reuses when a question matches.
					</p>
				</div>
				<div className="ml-auto flex items-center gap-1 rounded-full border border-white/80 bg-white/55 p-1 backdrop-blur-sm dark:border-white/10 dark:bg-white/[0.06]">
					<SelectButton
						theme={SelectButtonTheme.Switcher}
						selected={mode === 'sql'}
						onClick={() => setMode('sql')}
					>
						SQL
					</SelectButton>
					<SelectButton
						theme={SelectButtonTheme.Switcher}
						selected={mode === 'pql'}
						onClick={() => setMode('pql')}
					>
						PQL
					</SelectButton>
				</div>
				<div>
					<Button
						theme={ButtonTheme.Primary}
						size={Size.REGULAR}
						onClick={openCreateModal}
						iconPosition="left"
						shadow
					>
						<Icon name={IconName.Plus} className="h-4 w-4" />
						Create New {MODE_LABEL[mode]} Analysis
					</Button>
				</div>
			</header>

			<InfiniteScroll
				className="flex-1 px-6 py-6 sm:px-10"
				onLoadMore={loadMore}
				isLoading={isLoadingMore}
				hasMore={hasMore}
				error={items.length > 0 ? error : null}
			>
				{hasLoaded && (
					<SearchInput
						value={searchQuery}
						onChange={setSearchQuery}
						placeholder={`Search ${MODE_LABEL[mode]} Analyses…`}
						aria-label={`Search ${MODE_LABEL[mode]} analyses`}
						className="mb-6 w-full"
					/>
				)}

				{loading && (
					<div
						className="flex min-h-[calc(100dvh-7rem)] flex-col gap-4"
						role="status"
						aria-label={`Loading ${MODE_LABEL[mode]} analyses`}
					>
						{Array.from({ length: loadingSkeletonCount }).map((_, index) => (
							<SkeletonCard key={index} rows={4} />
						))}
					</div>
				)}

				{!loading && error != null && items.length === 0 && (
					<div className="mx-auto max-w-lg rounded-2xl border border-red-200/80 px-8 py-10 text-center dark:border-red-900/50 glass-strong">
						<h2 className="text-lg font-semibold tracking-tight text-red-800 dark:text-red-300">
							Couldn&apos;t load {MODE_LABEL[mode]} analyses
						</h2>
						<pre className="mt-4 max-w-full overflow-x-auto rounded-lg border border-red-100 bg-red-50/80 p-3 text-left text-xs text-red-900/80 dark:border-red-900/40 dark:bg-red-950/40 dark:text-red-200">
							{error}
						</pre>
						{/* `reload`, not `loadMore`: a failed first page leaves
						    nothing fetched, so `loadMore` sees the list as ended
						    and returns without asking for anything. */}
						<div className="mt-4 flex justify-center">
							<Button
								theme={ButtonTheme.Secondary}
								size={Size.SMALL}
								type="button"
								onClick={reload}
							>
								Retry
							</Button>
						</div>
					</div>
				)}

				{!loading && error == null && items.length === 0 && (
					<EmptyState
						variant={EmptyStateVariant.Borderless}
						title={
							debouncedSearchQuery
								? `No ${MODE_LABEL[mode]} Analyses Match Your Search`
								: `No ${MODE_LABEL[mode]} Analyses Created Yet`
						}
					/>
				)}

				{!loading && items.length > 0 && (
					<ul className="grid grid-cols-1 items-start gap-4 xl:grid-cols-2">
						{items.map((a) => (
							<li
								key={a.id}
								id={`analysis-${a.id}`}
								className={`flex flex-col rounded-card border glass p-5 ${
									a.id === focusId
										? 'border-primary ring-2 ring-primary/30 dark:border-primary'
										: 'border-white/70 dark:border-white/10'
								}`}
							>
								<div className="flex items-start justify-between gap-3">
									<div className="flex min-w-0 flex-1 items-center gap-2">
										<Icon
											name={IconName.ChartBar}
											className="h-4 w-4 shrink-0 text-body dark:text-zinc-300"
										/>
										<Text
											as="h2"
											text={a.name}
											variant={TextVariant.CardTitle}
											fill
										/>
									</div>
									<PopoverMenu
										className="shrink-0"
										items={[
											{
												label: 'Edit',
												icon: (
													<Icon
														name={IconName.Pencil}
														className="h-3.5 w-3.5"
													/>
												),
												onClick: () => openEditModal(a),
											},
											{
												label: 'Delete',
												icon: (
													<Icon
														name={IconName.Trash}
														className="h-3.5 w-3.5"
													/>
												),
												onClick: () => openDeleteModal(a),
												danger: true,
											},
										]}
										trigger={({ toggle }) => (
											<Button
												theme={ButtonTheme.IconNeutral}
												size={Size.SMALL}
												iconOnly
												type="button"
												onClick={toggle}
												aria-label={`Actions for ${a.name}`}
											>
												<Icon
													name={IconName.DotsVertical}
													className="h-4 w-4"
												/>
											</Button>
										)}
									/>
								</div>
								{a.description.trim() !== '' && (
									<div className="mt-2">
										<Text
											as="p"
											text={a.description}
											// Folded cards keep to one line so a long list stays
											// scannable; the full text comes with the details.
											lines={isExpanded(a.id) ? undefined : 1}
											variant={TextVariant.Body}
										/>
									</div>
								)}
								{isExpanded(a.id) && a.code.trim() !== '' && (
									<div id={`analysis-${a.id}-details`} className="mt-4">
										<SqlBlock sql={a.code} label={MODE_LABEL[mode]} />
									</div>
								)}
								{a.code.trim() !== '' && (
									<button
										type="button"
										aria-expanded={isExpanded(a.id)}
										aria-controls={`analysis-${a.id}-details`}
										onClick={() => toggleExpanded(a.id)}
										className="mt-3 inline-flex items-center gap-1.5 self-start rounded text-xs font-medium text-secondary transition-colors hover:text-primary focus-visible:ring-2 focus-visible:ring-primary focus-visible:outline-none dark:text-zinc-400 dark:hover:text-primary-soft"
									>
										<Icon
											name={IconName.ChevronRight}
											className={`h-3 w-3 transition-transform ${isExpanded(a.id) ? 'rotate-90' : ''}`}
										/>
										{isExpanded(a.id)
											? `Hide description and ${MODE_LABEL[mode]}`
											: `Show description and ${MODE_LABEL[mode]}`}
									</button>
								)}
							</li>
						))}
					</ul>
				)}
			</InfiniteScroll>

			<ModalCreateNewItem
				open={modalOpen}
				onClose={handleClose}
				title={
					isEditing
						? `Edit ${MODE_LABEL[mode]} Analysis`
						: `Create New ${MODE_LABEL[mode]} Analysis`
				}
				submitLabel={submitting ? 'Saving…' : 'Save'}
				onSubmit={handleSubmit}
				canSubmit={canSubmit}
				secondaryAction={
					isPql
						? undefined
						: {
								label: validating ? 'Validating…' : 'Validate SQL',
								onClick: handleValidateSql,
								disabled: trimmedCode.length === 0 || validating,
							}
				}
			>
				<div>
					<label className={FIELD_LABEL_CLASSNAME}>Name</label>
					<input
						type="text"
						value={name}
						onChange={(e) => setName(e.target.value)}
						placeholder={`${MODE_LABEL[mode]} Analysis Name`}
						className={FIELD_INPUT_CLASSNAME}
					/>
				</div>

				<div>
					<label className={FIELD_LABEL_CLASSNAME}>Description</label>
					<textarea
						value={description}
						onChange={(e) => setDescription(e.target.value)}
						placeholder="Add Short Description"
						rows={3}
						className={`resize-y ${FIELD_INPUT_CLASSNAME}`}
					/>
				</div>

				<div>
					<SqlEditor
						value={code}
						onChange={handleCodeChange}
						label={MODE_LABEL[mode]}
						placeholder={isPql ? 'PREDICT ...' : 'SELECT ...'}
					/>
				</div>

				{validationMessage != null && (
					<p className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-sm text-body dark:border-zinc-700 dark:bg-zinc-900/60 dark:text-zinc-300">
						{validationMessage}
					</p>
				)}

				{submitError != null && (
					<p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-300">
						{submitError}
					</p>
				)}
			</ModalCreateNewItem>

			<ConfirmModal
				open={deletingItem !== null}
				onCancel={handleDeleteClose}
				onConfirm={handleDeleteConfirm}
				title={`Delete ${MODE_LABEL[mode]} Analysis`}
				message={
					<>
						Are you sure you want to delete <strong>{deletingItem?.name}</strong>? This
						action cannot be undone.
					</>
				}
				confirming={deleting}
				error={deleteError}
			/>
		</div>
	);
};
