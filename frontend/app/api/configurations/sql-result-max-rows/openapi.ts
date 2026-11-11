// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { z } from 'zod';
import {
	DEFAULT_SQL_RESULT_MAX_ROWS,
	MAX_SQL_RESULT_MAX_ROWS,
	MIN_SQL_RESULT_MAX_ROWS,
} from '@/lib/sqlResultMaxRows';
import type { OpenApiRoute } from '@/types/openapi';

// Exported so route.ts enforces exactly what this spec advertises, rather than
// the two drifting apart.
export const sqlResultMaxRowsBody = z.object({
	rows: z.number().int().min(MIN_SQL_RESULT_MAX_ROWS).max(MAX_SQL_RESULT_MAX_ROWS),
});

const sqlResultMaxRowsSchema = z.object({
	rows: z.number().int().describe('Maximum rows one statement may return.'),
});

export const openapi: OpenApiRoute = {
	get: {
		responses: {
			200: {
				description:
					'How many rows a SQL statement issued by the text-to-SQL agent may return; ' +
					`a longer result is cut and reported as such. ${DEFAULT_SQL_RESULT_MAX_ROWS} when unset.`,
				schema: sqlResultMaxRowsSchema,
			},
		},
	},
	put: {
		body: {
			description: `\`rows\` must be an integer from ${MIN_SQL_RESULT_MAX_ROWS} to ${MAX_SQL_RESULT_MAX_ROWS}.`,
			schema: sqlResultMaxRowsBody,
		},
		responses: {
			200: {
				description: 'The stored limit. Applies from the next statement.',
				schema: sqlResultMaxRowsSchema,
			},
			400: { description: 'Body is not JSON, or `rows` is not an in-range integer.' },
		},
	},
};
