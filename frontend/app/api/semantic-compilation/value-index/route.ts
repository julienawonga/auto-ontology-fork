// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { withPermission } from '@/auth/with-auth';
import { proxyToBackend } from '@/auth/proxy-backend';

// Copy the distinct values of one database's small text columns into the
// semantic index (?database_name=) so a question's wording can be matched to
// how the data spells it. It moves data out of the source database into the
// vector index and runs the embedding endpoint over every value, so it sits
// behind `manage` like the reindex. No settings page calls it yet.
export const POST = withPermission({ semanticCompilation: ['manage'] })((req) =>
	proxyToBackend(req),
);
