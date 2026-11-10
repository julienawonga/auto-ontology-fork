// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

/**
 * Where the chat display settings live in the query cache.
 *
 * Not keyed by account: the setting is instance-wide and answers every role
 * the same.
 */

import { queryOptions, type QueryClient } from '@tanstack/react-query';

import { chatDetailsApi } from '@/api/settings';

const DETAILS_EXPANDED_KEY = ['chatDisplay', 'detailsExpanded'] as const;

export const chatDisplayQueries = {
	/** Whether answers show their explanation and SQL expanded by default. */
	detailsExpanded: () =>
		queryOptions({
			queryKey: DETAILS_EXPANDED_KEY,
			queryFn: async (): Promise<boolean> => {
				const res = await chatDetailsApi.get();
				if (res.error) throw new Error(res.message ?? 'Failed to load chat settings.');
				return res.enabled;
			},
		}),
};

/** Stores the value an admin just saved, so open chat views follow it. */
export const setChatDetailsExpanded = (queryClient: QueryClient, enabled: boolean): void => {
	queryClient.setQueryData(DETAILS_EXPANDED_KEY, enabled);
};
