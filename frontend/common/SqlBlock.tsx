// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useMemo } from 'react';
import { CopyButton } from '@/common/Button';
import { tokenizeSql, type SqlTokenKind } from '@/lib/sqlHighlight';

/**
 * The surface every code block shares: light and translucent in the light
 * theme, dark in the dark one, so code reads like the page around it.
 */
export const codeSurfaceClass =
	'border border-zinc-900/10 bg-white/70 dark:border-white/10 dark:bg-black/40';
export const codeHeaderClass =
	'flex items-center justify-between border-b border-zinc-900/10 px-3 py-1.5 dark:border-white/10';
export const codeLabelClass = 'text-xs font-medium text-secondary dark:text-zinc-400';
export const codeTextClass = 'text-glass-text dark:text-zinc-100';

const TOKEN_CLASS: Record<SqlTokenKind, string> = {
	keyword: 'font-semibold text-primary dark:text-primary-soft',
	string: 'text-emerald-700 dark:text-emerald-300',
	number: 'text-amber-700 dark:text-amber-300',
	comment: 'italic text-secondary dark:text-zinc-500',
	plain: '',
};

export type SqlBlockProps = {
	sql: string;
	label?: string;
	className?: string;
};

export const SqlBlock = ({ sql, label = 'SQL', className }: SqlBlockProps) => {
	const tokens = useMemo(() => tokenizeSql(sql), [sql]);

	return (
		<div
			className={`group relative overflow-hidden rounded-xl ${codeSurfaceClass} ${className ?? ''}`}
		>
			<div className={codeHeaderClass}>
				<span className={codeLabelClass}>{label}</span>
				<CopyButton text={sql} />
			</div>
			<pre
				className={`p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap wrap-anywhere ${codeTextClass}`}
			>
				<code>
					{tokens.map((token, i) =>
						token.kind === 'plain' ? (
							token.text
						) : (
							<span key={i} className={TOKEN_CLASS[token.kind]}>
								{token.text}
							</span>
						),
					)}
				</code>
			</pre>
		</div>
	);
};

export type SqlEditorProps = {
	value: string;
	onChange: (value: string) => void;
	label?: string;
	placeholder?: string;
	rows?: number;
	className?: string;
};

export const SqlEditor = ({
	value,
	onChange,
	label = 'SQL',
	placeholder = 'SELECT ...',
	rows = 8,
	className,
}: SqlEditorProps) => (
	<div
		className={`overflow-hidden rounded-xl transition-colors focus-within:ring-2 focus-within:ring-primary/30 ${codeSurfaceClass} ${className ?? ''}`}
	>
		<div className={codeHeaderClass}>
			<span className={codeLabelClass}>{label}</span>
		</div>
		<textarea
			value={value}
			onChange={(e) => onChange(e.target.value)}
			placeholder={placeholder}
			rows={rows}
			spellCheck={false}
			aria-label={label}
			className={`block w-full resize-y bg-transparent p-3 font-mono text-xs leading-relaxed outline-none placeholder:text-secondary dark:placeholder:text-zinc-500 ${codeTextClass}`}
		/>
	</div>
);
