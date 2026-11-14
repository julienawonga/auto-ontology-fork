// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { Button } from '@/common/Button';
import { ButtonTheme, Size } from '@/enums/button';

export const formatGenerationButtonText = (isGenerating: boolean): string =>
	isGenerating
		? 'Grouping Terms... (this may take 10-60s)'
		: "Group this database's Terms by business domain";

export const getBannerHelpText = (): string =>
	"Group this database's Terms into business domains for a higher-level view.";

type DomainGenerationBannerProps = {
	onGenerate: () => void;
	isGenerating: boolean;
	error?: string | null;
	canEdit?: boolean;
};

export const DomainGenerationBanner = ({
	onGenerate,
	isGenerating,
	error,
	canEdit = true,
}: DomainGenerationBannerProps) => {
	return (
		<div className="pointer-events-auto absolute left-1/2 top-16 z-20 -translate-x-1/2 rounded-xl border border-primary/20 bg-white/90 p-4 shadow-lg backdrop-blur dark:border-primary/30 dark:bg-zinc-900/90">
			<div className="flex flex-col items-center gap-2 text-center">
				<p className="text-sm font-medium text-body dark:text-zinc-200">
					{getBannerHelpText()}
				</p>
				{error && <p className="text-xs text-red-600 dark:text-red-400">{error}</p>}
				{canEdit && (
					<Button
						theme={ButtonTheme.Primary}
						size={Size.REGULAR}
						type="button"
						disabled={isGenerating}
						onClick={onGenerate}
						iconPosition="left"
					>
						{formatGenerationButtonText(isGenerating)}
					</Button>
				)}
			</div>
		</div>
	);
};
