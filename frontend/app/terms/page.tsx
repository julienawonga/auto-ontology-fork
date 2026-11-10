// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { Suspense } from 'react';
import { TermsLoadingSkeleton, TermsView } from '@/components/termsPage';

export default function TermsPage() {
	return (
		<Suspense
			fallback={
				<div className="flex h-full w-full flex-col">
					<header className="flex items-center gap-3 px-6 pt-8 pb-2 sm:px-10">
						<h1 className="text-[28px] font-bold tracking-tight text-glass-text dark:text-zinc-50 leading-tight">
							Terms
						</h1>
					</header>
					<div className="flex-1 px-6 py-6">
						<TermsLoadingSkeleton />
					</div>
				</div>
			}
		>
			<TermsView />
		</Suspense>
	);
}
