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
 * It shows only when the page has gone below its section (a database, a term,
 * a tag) or fills the slot. A section on its own only repeats the page title,
 * and the section a visit came from is not worth a line of its own.
 */
export const PageBar = () => {
	const { items, pageTrail, rightSlot } = useBreadcrumbs();
	if (pageTrail.length === 0 && !rightSlot) return null;

	return (
		<div className="flex h-11 shrink-0 items-center gap-3 px-6 pt-2 sm:px-10">
			<Breadcrumbs items={pageTrail.length === 0 ? [] : items} />
			{rightSlot ? <div className="ml-auto flex items-center gap-2">{rightSlot}</div> : null}
		</div>
	);
};
