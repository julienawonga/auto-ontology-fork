// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

/**
 * Questions the chat offers before the first message.
 *
 * Drawn from the saved SQL analyses rather than a hand-kept list: an analysis
 * is a question someone already answered with a certified query, so asking it
 * routes the agent straight to that definition. The list follows whatever the
 * Analysis page holds, and is empty when it holds nothing.
 */

import { queryOptions } from '@tanstack/react-query';

import { analyses } from '@/api/analyses';

/** How many suggestions the empty chat shows. */
const SUGGESTION_COUNT = 4;

export const chatSuggestionQueries = {
	fromAnalyses: () =>
		queryOptions({
			queryKey: ['chatSuggestions', 'analyses', SUGGESTION_COUNT] as const,
			queryFn: async (): Promise<string[]> => {
				const res = await analyses.list({ skip: 0, limit: SUGGESTION_COUNT });
				// Suggestions are a convenience: a failed read shows none rather
				// than an error in the middle of the chat.
				if (res.error) return [];
				return (res.data ?? []).map((a) => a.name.trim()).filter(Boolean);
			},
			staleTime: 5 * 60 * 1000,
		}),
};
