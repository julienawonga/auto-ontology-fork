// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { withPermission } from '@/auth/with-auth';
import { proxyToBackend } from '@/auth/proxy-backend';

// semanticCompilationApi.reindex — re-embed one database's Terms and
// ColumnAttributes into the semantic index (?database_name=). It rewrites
// index rows rather than deleting any ontology data, but it runs the embedding
// endpoint over the whole database, so it sits behind `manage` like the reset.
export const POST = withPermission({ semanticCompilation: ['manage'] })((req) =>
	proxyToBackend(req),
);
