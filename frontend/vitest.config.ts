// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

// Node environment: the tests cover pure logic and server-rendered markup, so
// no browser or DOM library is needed. `@/` mirrors the tsconfig path alias.
export default defineConfig({
	resolve: {
		alias: { '@': fileURLToPath(new URL('.', import.meta.url)) },
	},
	test: {
		environment: 'node',
		include: ['**/*.test.{ts,tsx}'],
		exclude: ['node_modules/**', '.next/**'],
	},
});
