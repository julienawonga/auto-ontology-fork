// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useSyncExternalStore } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Icon, IconName } from '@/common/icons';
import { readNavOpen, readNavOpenOnServer, setNavOpen, subscribeToNavOpen } from '@/lib/navRail';

type NavItem = {
	icon: IconName;
	href: string;
	label: string;
	adminOnly?: boolean;
};

const navItems: NavItem[] = [
	{ icon: IconName.ChatBubble, href: '/chat', label: 'Chat' },
	{ icon: IconName.Discovery, href: '/discovery', label: 'Discovery' },
	{ icon: IconName.Terms, href: '/terms', label: 'Terms' },
	{ icon: IconName.ChartBar, href: '/analysis', label: 'Analysis' },
	{ icon: IconName.Exploration, href: '/exploration', label: 'Exploration' },
	{ icon: IconName.Database, href: '/data', label: 'Data' },
	{ icon: IconName.ChartLine, href: '/analytics', label: 'Analytics', adminOnly: true },
	// Settings is admin-only and now also contains Users and Single Sign-On
	// (see app/settings/*). Viewers don't see it.
	{ icon: IconName.Settings, href: '/settings', label: 'Settings', adminOnly: true },
];

/**
 * The left navigation: open (icons and labels) or collapsed (icons only),
 * switched by the button at its top, the way chat apps do it. The page always
 * sits beside it. The choice is a per-browser preference.
 */
export const NavRail = ({ isAdmin = false }: { isAdmin?: boolean }) => {
	const pathname = usePathname();
	const isExpanded = useSyncExternalStore(subscribeToNavOpen, readNavOpen, readNavOpenOnServer);
	const visibleItems = navItems.filter((item) => !item.adminOnly || isAdmin);

	return (
		<nav
			aria-label="Main"
			className={`flex h-full shrink-0 flex-col overflow-hidden border-r border-zinc-200 bg-white py-3 transition-[width] duration-200 ease-out dark:border-zinc-800 dark:bg-zinc-950 ${
				isExpanded ? 'w-56' : 'w-12'
			}`}
		>
			<div className={`mb-2 flex ${isExpanded ? 'justify-end px-1.5' : 'justify-center'}`}>
				<button
					type="button"
					onClick={() => setNavOpen(!isExpanded)}
					aria-expanded={isExpanded}
					aria-label={isExpanded ? 'Collapse navigation' : 'Expand navigation'}
					title={isExpanded ? 'Collapse navigation' : 'Expand navigation'}
					className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-secondary transition-colors hover:bg-zinc-100 hover:text-heading focus-visible:ring-2 focus-visible:ring-primary focus-visible:outline-none dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
				>
					<Icon name={IconName.Sidebar} className="h-5 w-5" />
				</button>
			</div>

			<div className={`flex flex-1 flex-col gap-2 ${isExpanded ? 'px-1.5' : 'items-center'}`}>
				{visibleItems.map((item) => {
					const isActive = pathname.startsWith(item.href);

					return (
						<Link
							key={item.href}
							href={item.href}
							title={isExpanded ? undefined : item.label}
							className={`flex h-9 shrink-0 items-center gap-3 rounded-lg transition-colors ${
								isExpanded ? 'w-full px-2' : 'w-9 justify-center'
							} ${
								isActive
									? 'bg-primary/15 text-primary'
									: 'text-body hover:bg-zinc-100 hover:text-heading dark:hover:bg-zinc-800 dark:hover:text-zinc-200'
							}`}
						>
							<Icon name={item.icon} className="h-5 w-5 shrink-0" />
							{isExpanded && (
								<span className="truncate text-sm font-medium whitespace-nowrap">
									{item.label}
								</span>
							)}
						</Link>
					);
				})}
			</div>
		</nav>
	);
};
