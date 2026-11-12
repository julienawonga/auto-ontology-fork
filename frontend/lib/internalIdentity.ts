// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

export const INTERNAL_USER_HEADER = 'x-auto-ontology-user-id';
export const INTERNAL_TOKEN_HEADER = 'x-internal-token';

/**
 * Headers for every server-side call to the Python services (the FastAPI API
 * and, through it, the ingestion service).
 *
 * The services trust the user identity this carries, so they only accept it
 * from a caller that also proves it is the web app, with the shared
 * `INTERNAL_API_TOKEN`. Every such call must go through here: a call that
 * forgets the token is refused as soon as an operator turns the token on.
 * `lib/internalIdentity.test.ts` fails when a file that targets the Python API
 * does not use this.
 *
 * Without a token configured the services accept unauthenticated calls, so the
 * token header is simply omitted. It is read per call, not at import, so it is
 * never captured into a client bundle and tests can set it. It is a plain
 * `process.env` read (never `NEXT_PUBLIC_*`), so Next.js leaves it undefined in
 * the browser bundle instead of inlining a value.
 */
export const buildInternalHeaders = (userId?: string): Record<string, string> => {
	const token = process.env.INTERNAL_API_TOKEN;
	return {
		...(token ? { [INTERNAL_TOKEN_HEADER]: token } : {}),
		...(userId ? { [INTERNAL_USER_HEADER]: userId } : {}),
	};
};
