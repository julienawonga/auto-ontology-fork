// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { CopyButton } from '@/common/Button';
import {
	codeHeaderClass,
	codeLabelClass,
	codeSurfaceClass,
	codeTextClass,
} from '@/common/SqlBlock';

export type PropertyRowProps = {
	label: string;
	value: string;
	monospace?: boolean;
	className?: string;
};

export const PropertyRow = ({ label, value, monospace = false, className }: PropertyRowProps) => (
	<div
		className={`group relative overflow-hidden rounded-xl ${codeSurfaceClass} ${className ?? ''}`}
	>
		<div className={codeHeaderClass}>
			<span className={codeLabelClass}>{label}</span>
			{value !== '' && <CopyButton text={value} />}
		</div>
		<p
			className={`p-3 text-xs leading-relaxed wrap-anywhere ${codeTextClass} ${
				monospace ? 'font-mono' : ''
			}`}
		>
			{value || '—'}
		</p>
	</div>
);
