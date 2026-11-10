// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import type { SemanticIndexEntry, SemanticReindexResult } from '@/types/semanticIndex';

/** How one database's index coverage reads on Settings > Semantic Compilation. */
export const describeIndexEntry = (
	entry: SemanticIndexEntry,
): { complete: boolean; label: string } => {
	if (entry.missing > 0) {
		return { complete: false, label: `${entry.missing} of ${entry.expected} missing` };
	}
	return {
		complete: true,
		label: entry.expected === 0 ? 'Nothing to index' : `Complete (${entry.indexed} indexed)`,
	};
};

/** Message and tone for a reindex that went through. Fewer written than expected is a warning. */
export const describeReindexResult = ({
	database_name,
	expected,
	written,
}: SemanticReindexResult): { complete: boolean; message: string } =>
	written < expected
		? {
				complete: false,
				message: `${database_name}: only ${written} of ${expected} entries were indexed. Check the embedding endpoint and rebuild again.`,
			}
		: {
				complete: true,
				message: `${database_name}: index rebuilt, ${written} of ${expected} entries written.`,
			};

/** Human text for a refused reindex; `detail` is the backend's own message, if any. */
export const reindexErrorMessage = (status: number, detail: string | null): string => {
	switch (status) {
		case 404:
			return 'This database is no longer known to Lexis. Reload the page.';
		case 409:
			return 'A semantic compilation is running. Wait for it to finish, then rebuild the index.';
		case 503:
			return 'The embedding model is not configured, so the index cannot be rebuilt.';
		case 403:
			return 'You do not have permission to rebuild the index.';
		default:
			return detail ?? 'Failed to rebuild the index. Please try again.';
	}
};
