// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { Breadcrumbs } from '@/common/Breadcrumbs';
import { useBreadcrumbs } from '@/contexts/BreadcrumbContext';

/**
 * A slim line above the page content: the breadcrumb trail and any actions a
 * page puts in the slot. The logo, search and account live in the sidebar.
 *
 * A one-level trail only repeats the page's own title, so the line shows from
 * two levels up (a tag, a database, a term), or when a page fills the slot.
 */
export const PageBar = () => {
	const { items, rightSlot } = useBreadcrumbs();
	if (items.length < 2 && !rightSlot) return null;

	return (
		<div className="flex h-11 shrink-0 items-center gap-3 px-6 pt-2 sm:px-10">
			<Breadcrumbs items={items.length < 2 ? [] : items} />
			{rightSlot ? <div className="ml-auto flex items-center gap-2">{rightSlot}</div> : null}
		</div>
	);
};
