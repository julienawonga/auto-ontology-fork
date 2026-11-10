// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useEffect, useState, useSyncExternalStore } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Icon, IconName } from '@/common/icons';
import { GlobalSearchModal } from '@/common/GlobalSearch';
import { UserMenu } from '@/common/UserMenu';
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

// The shortcut label follows the platform; the server renders the common one.
const subscribeToNothing = () => () => {};
const readShortcutLabel = () => (/Mac|iPhone|iPad/.test(navigator.platform) ? '⌘K' : 'Ctrl K');
const readShortcutLabelOnServer = () => 'Ctrl K';

const iconButtonClass =
	'flex h-10 w-10 shrink-0 items-center justify-center rounded-xl transition-colors focus-visible:ring-2 focus-visible:ring-primary focus-visible:outline-none';

/**
 * The sidebar: logo, search, the main navigation and the signed-in account.
 *
 * Open, it shows labels; collapsed, icons only. The toggle sits at its top
 * (the logo, when collapsed), the way chat and dashboard apps do it, and the
 * choice is a per-browser preference. The page always sits beside it.
 */
export const NavRail = ({ isAdmin = false, version }: { isAdmin?: boolean; version?: string }) => {
	const pathname = usePathname();
	const isExpanded = useSyncExternalStore(subscribeToNavOpen, readNavOpen, readNavOpenOnServer);
	const shortcutLabel = useSyncExternalStore(
		subscribeToNothing,
		readShortcutLabel,
		readShortcutLabelOnServer,
	);
	const [searchOpen, setSearchOpen] = useState(false);
	const visibleItems = navItems.filter((item) => !item.adminOnly || isAdmin);

	// Ctrl+K / Cmd+K opens search from anywhere in the app.
	useEffect(() => {
		const onKeyDown = (event: KeyboardEvent) => {
			if (event.key.toLowerCase() !== 'k' || !(event.ctrlKey || event.metaKey)) return;
			event.preventDefault();
			setSearchOpen(true);
		};
		window.addEventListener('keydown', onKeyDown);
		return () => window.removeEventListener('keydown', onKeyDown);
	}, []);

	return (
		<aside
			className={`flex h-full shrink-0 flex-col gap-1 overflow-hidden border-r border-white/70 bg-white/45 py-3.5 backdrop-blur-xl transition-[width] duration-200 ease-out dark:border-white/10 dark:bg-white/[0.03] ${
				isExpanded ? 'w-62 px-2.5' : 'w-16 items-center px-2'
			}`}
		>
			{isExpanded ? (
				<div className="flex h-10 items-center gap-2.5 pr-1 pl-2">
					<Icon name={IconName.AppLogo} className="h-5.5 w-5.5 shrink-0 text-primary" />
					<span className="flex-1 truncate text-sm font-bold text-heading dark:text-zinc-100">
						Auto Ontology
					</span>
					<button
						type="button"
						onClick={() => setNavOpen(false)}
						aria-expanded
						aria-label="Collapse navigation"
						title="Collapse navigation"
						className={`${iconButtonClass} h-9 w-9 text-secondary hover:bg-white/70 hover:text-heading dark:text-zinc-400 dark:hover:bg-white/[0.08] dark:hover:text-zinc-100`}
					>
						<Icon name={IconName.Sidebar} className="h-5 w-5" />
					</button>
				</div>
			) : (
				<button
					type="button"
					onClick={() => setNavOpen(true)}
					aria-expanded={false}
					aria-label="Expand navigation"
					title="Expand navigation"
					className={`${iconButtonClass} hover:bg-white/70 dark:hover:bg-white/[0.08]`}
				>
					<Icon name={IconName.AppLogo} className="h-5.5 w-5.5 text-primary" />
				</button>
			)}

			<button
				type="button"
				onClick={() => setSearchOpen(true)}
				aria-label={`Search (${shortcutLabel})`}
				title={isExpanded ? undefined : `Search (${shortcutLabel})`}
				className={`my-2 flex h-10 shrink-0 items-center gap-2.5 rounded-xl border border-white/85 bg-white/70 text-sm text-secondary transition-colors hover:bg-white/90 focus-visible:ring-2 focus-visible:ring-primary focus-visible:outline-none dark:border-white/10 dark:bg-white/[0.06] dark:text-zinc-400 dark:hover:bg-white/[0.1] ${
					isExpanded ? 'w-full pr-2.5 pl-3 text-left' : 'w-10 justify-center'
				}`}
			>
				<Icon name={IconName.Discovery} className="h-4 w-4 shrink-0" />
				{isExpanded && (
					<>
						<span className="flex-1">Search…</span>
						<kbd className="rounded-md border border-zinc-900/15 bg-white/80 px-1.5 py-0.5 font-sans text-[11px] dark:border-white/15 dark:bg-white/[0.06]">
							{shortcutLabel}
						</kbd>
					</>
				)}
			</button>

			<nav
				aria-label="Main"
				className={`flex flex-1 flex-col gap-1 ${isExpanded ? '' : 'items-center'}`}
			>
				{visibleItems.map((item) => {
					const isActive = pathname.startsWith(item.href);

					return (
						<Link
							key={item.href}
							href={item.href}
							aria-current={isActive ? 'page' : undefined}
							title={isExpanded ? undefined : item.label}
							className={`flex h-10 shrink-0 items-center gap-3 rounded-xl transition-colors focus-visible:ring-2 focus-visible:ring-primary focus-visible:outline-none ${
								isExpanded ? 'w-full px-3' : 'w-10 justify-center'
							} ${
								isActive
									? 'bg-primary/12 font-semibold text-primary dark:bg-primary/20 dark:text-primary-soft'
									: 'font-medium text-body hover:bg-white/70 hover:text-heading dark:text-zinc-300 dark:hover:bg-white/[0.08] dark:hover:text-zinc-100'
							}`}
						>
							<Icon name={item.icon} className="h-4.5 w-4.5 shrink-0" />
							{isExpanded && (
								<span className="truncate text-sm whitespace-nowrap">
									{item.label}
								</span>
							)}
						</Link>
					);
				})}
			</nav>

			<UserMenu version={version} compact={!isExpanded} />

			<GlobalSearchModal open={searchOpen} onClose={() => setSearchOpen(false)} />
		</aside>
	);
};
