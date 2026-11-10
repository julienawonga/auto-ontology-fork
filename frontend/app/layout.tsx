// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import type { Metadata } from 'next';
import { headers } from 'next/headers';
import { JetBrains_Mono, Plus_Jakarta_Sans } from 'next/font/google';
import { TooltipProvider } from '@nvidia/foundations-react-core';
import {
	HydrationBoundary,
	QueryClient,
	defaultShouldDehydrateQuery,
	dehydrate,
	type DehydratedState,
} from '@tanstack/react-query';
import { NavRail } from '@/common/NavRail';
import { PageBar } from '@/common/PageBar';
import { ThemeSync } from '@/common/ThemeSwitcher';
import { BreadcrumbProvider } from '@/contexts/BreadcrumbContext';
import { QueryProvider } from '@/contexts/QueryProvider';
import { getCurrentSession } from '@/auth/auth-guards';
import { Role } from '@/enums/auth';
import { connectionQueries } from '@/lib/queries/connections';
import { tagQueries } from '@/lib/queries/tags';
import { zoneQueries } from '@/lib/queries/zones';
import { THEME_INIT_SCRIPT } from '@/lib/theme';
import './globals.css';

const isLoginOrOauthPath = (pathname: string): boolean =>
	pathname === '/login' ||
	pathname.startsWith('/login/') ||
	pathname === '/invite' ||
	pathname.startsWith('/invite/') ||
	pathname === '/oauth' ||
	pathname.startsWith('/oauth/');

// Mapped to Tailwind's `font-sans` and `font-mono` in globals.css.
const plusJakarta = Plus_Jakarta_Sans({
	variable: '--font-plus-jakarta',
	subsets: ['latin'],
});

const jetbrainsMono = JetBrains_Mono({
	variable: '--font-jetbrains-mono',
	subsets: ['latin'],
});

export const metadata: Metadata = {
	title: {
		default: 'Lexis',
		template: 'Lexis - %s',
	},
	description: 'Lexis',
	icons: {
		icon: '/favicon.svg',
	},
};

/**
 * The reads the whole app shares, started here so they arrive with the page
 * rather than after it: this layout survives every client navigation, so one
 * pass covers the session, not each route inside it.
 *
 * The zone list is asked for on the user's behalf, which is why the id goes
 * in: `zones` answers with what that account may see, and the cache keys it
 * the same way. Connections carry no such scope — the list route strips
 * credentials and answers every role alike — so one entry serves the session.
 *
 * All three are read here rather than in the section that happens to show
 * them: they describe the deployment rather than a page, and a picker that
 * has to wait for its own read is the thing this exists to remove. The cost
 * is three internal reads per *full* page load — client navigations reuse
 * them — and none of the three is awaited.
 *
 * That is also why the dehydrated state has to carry the pending queries
 * themselves, dehydrated alongside what `defaultShouldDehydrateQuery` already
 * keeps: the markup is not held back for lists the first screen may not even
 * show, and the browser subscribes to reads already in flight instead of
 * starting its own.
 */
const prefetchSharedData = (userId: string): DehydratedState => {
	const queryClient = new QueryClient();
	void queryClient.prefetchQuery(tagQueries.vocabulary());
	void queryClient.prefetchQuery(zoneQueries.list(userId));
	void queryClient.prefetchQuery(connectionQueries.list());
	return dehydrate(queryClient, {
		shouldDehydrateQuery: (query) =>
			defaultShouldDehydrateQuery(query) || query.state.status === 'pending',
	});
};

export default async function RootLayout({
	children,
}: Readonly<{
	children: React.ReactNode;
}>) {
	const session = await getCurrentSession();
	const isAdmin = session?.user.role === Role.Admin;
	// Injected at runtime by the Helm chart from the chart version (.Chart.Version).
	const appVersion = process.env.APP_VERSION;
	const pathname = (await headers()).get('x-auto-ontology-pathname') ?? '';
	const showAppChrome = Boolean(session) && !isLoginOrOauthPath(pathname);
	// Only read where `showAppChrome` holds, which is where there is a session.
	const userId = session?.user.id ?? '';

	return (
		<html
			lang="en"
			className={`${plusJakarta.variable} ${jetbrainsMono.variable} h-full antialiased`}
			// The inline theme script adds `dark` and a color-scheme style before
			// React hydrates; let the DOM win for this element.
			suppressHydrationWarning
		>
			<head>
				{/* Sets the theme class before the first paint, so there is no flash. */}
				<script dangerouslySetInnerHTML={{ __html: THEME_INIT_SCRIPT }} />
			</head>
			<body className="relative isolate flex h-full flex-col overflow-hidden bg-glass-canvas font-sans text-heading dark:bg-glass-canvas-dark dark:text-zinc-100">
				{/* Soft colour fields behind every page: the frosted panels need
				    something to blur, or the glass reads as plain grey. */}
				<div aria-hidden className="pointer-events-none fixed inset-0 -z-10">
					<div className="absolute -top-40 right-[-10%] h-[560px] w-[560px] rounded-full bg-primary/20 blur-3xl dark:bg-primary/25" />
					<div className="absolute bottom-[-20%] left-[15%] h-[480px] w-[480px] rounded-full bg-glass-secondary/15 blur-3xl dark:bg-primary-soft/10" />
				</div>
				<ThemeSync />
				<QueryProvider>
					<TooltipProvider openDelayDuration={400} skipDelayDuration={0}>
						{showAppChrome ? (
							// Only the signed-in app reads the shared lists; the login
							// and consent screens have no picker to fill and no session
							// to read them with.
							<HydrationBoundary state={prefetchSharedData(userId)}>
								<BreadcrumbProvider>
									<div className="flex min-h-0 flex-1">
										<NavRail isAdmin={isAdmin} version={appVersion} />
										<div className="flex min-w-0 flex-1 flex-col">
											<PageBar />
											<div className="min-h-0 flex-1">{children}</div>
										</div>
									</div>
								</BreadcrumbProvider>
							</HydrationBoundary>
						) : (
							children
						)}
					</TooltipProvider>
				</QueryProvider>
			</body>
		</html>
	);
}
