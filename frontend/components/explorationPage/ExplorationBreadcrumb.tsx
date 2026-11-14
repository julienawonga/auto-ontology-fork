// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

type ExplorationBreadcrumbProps = {
	databaseName: string;
	domainName?: string;
	onAllDatabases: () => void;
	onDatabase: () => void;
};

export const ExplorationBreadcrumb = ({
	databaseName,
	domainName,
	onAllDatabases,
	onDatabase,
}: ExplorationBreadcrumbProps) => (
	<nav
		aria-label="Exploration breadcrumb"
		className="flex items-center gap-2 text-sm text-secondary"
	>
		<button type="button" onClick={onAllDatabases} className="hover:underline">
			All databases
		</button>
		<span aria-hidden="true">&gt;</span>
		<button type="button" onClick={onDatabase} className="hover:underline">
			{databaseName}
		</button>
		{domainName && (
			<>
				<span aria-hidden="true">&gt;</span>
				<span aria-current="page" className="text-heading">
					{domainName}
				</span>
			</>
		)}
	</nav>
);
