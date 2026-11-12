// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { ontologyLanguageOptionsFor } from '@/lib/ontologyLanguage';

export type OntologyLanguageFieldProps = {
	value: string;
	onChange: (code: string) => void;
	/** Editing an existing connection: the glossary is not rebuilt by a change. */
	editing?: boolean;
};

export const OntologyLanguageField = ({
	value,
	onChange,
	editing = false,
}: OntologyLanguageFieldProps) => (
	<label className="flex flex-col gap-1.5">
		<span className="text-sm font-medium text-heading dark:text-zinc-200">
			Ontology language
		</span>
		<select
			value={value}
			onChange={({ target }) => onChange(target.value)}
			data-testid="connection-field-ontology_language"
			className="rounded-xl border border-zinc-900/15 bg-white/70 px-3 py-2 text-sm text-heading outline-none focus:border-primary focus:ring-2 focus:ring-primary/30 dark:border-white/15 dark:bg-white/[0.06] dark:text-zinc-100"
		>
			{ontologyLanguageOptionsFor(value).map((option) => (
				<option key={option.code} value={option.code}>
					{option.code === 'en' ? `${option.label} (default)` : option.label}
				</option>
			))}
		</select>
		<span className="text-xs text-secondary dark:text-zinc-400">
			The language the business glossary for this database is written in; questions in other
			languages are translated automatically.
		</span>
		{editing && (
			<span className="text-xs text-secondary dark:text-zinc-400">
				Changing this does not rebuild the existing glossary.
			</span>
		)}
	</label>
);
