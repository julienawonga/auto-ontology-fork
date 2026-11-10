// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import type { Metadata } from 'next';
import { JetBrains_Mono, Plus_Jakarta_Sans } from 'next/font/google';
import { requireUser } from '@/auth/auth-guards';

export const metadata: Metadata = {
	title: 'Chat',
};

// The glass design trial is scoped to the Chat page, so its faces load here
// rather than in the root layout.
const plusJakarta = Plus_Jakarta_Sans({
	variable: '--font-plus-jakarta',
	subsets: ['latin'],
});

const jetbrainsMono = JetBrains_Mono({
	variable: '--font-jetbrains-mono',
	subsets: ['latin'],
});

export default async function ChatLayout({ children }: { children: React.ReactNode }) {
	// Redirect to /login when there's no valid session (e.g. an expired or
	// rotated-secret cookie) instead of rendering the chat shell unauthenticated.
	await requireUser();
	return (
		// Code blocks read the preflight's mono variable, so it is pointed at the
		// chat's mono face here too.
		<div
			className={`${plusJakarta.variable} ${jetbrainsMono.variable} h-full font-glass [--default-mono-font-family:var(--font-jetbrains-mono),ui-monospace,monospace]`}
		>
			{children}
		</div>
	);
}
