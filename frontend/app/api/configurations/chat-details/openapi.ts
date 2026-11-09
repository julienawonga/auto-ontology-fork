// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { z } from 'zod';
import { enabledFlagSchema } from '@/lib/openapiSchemas';
import type { OpenApiRoute } from '@/types/openapi';

export const openapi: OpenApiRoute = {
	get: {
		responses: {
			200: {
				description:
					'Whether chat answers show their explanation and SQL expanded. Off when ' +
					'unset, so a fresh instance reports `false` and answers start collapsed.',
				schema: enabledFlagSchema,
			},
		},
	},
	put: {
		body: {
			description: 'Anything other than `true` turns the flag off.',
			schema: z.object({ enabled: z.boolean() }),
		},
		responses: {
			200: { description: 'The stored flag.', schema: enabledFlagSchema },
		},
	},
};
