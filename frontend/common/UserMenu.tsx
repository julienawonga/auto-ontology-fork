// SPDX-FileCopyrightText: Copyright (c) 2024-2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { signOut, useSession } from '@/auth/auth-client';
import { userCan } from '@/auth/permissions';
import { Icon, IconName } from '@/common/icons';
import { PopoverMenu } from '@/common/PopoverMenu';
import { Text } from '@/common/Text';
import { ThemeSwitcher } from '@/common/ThemeSwitcher';
import { PopoverAlign } from '@/enums/popover';
import { TextVariant } from '@/enums/text';

type UserMenuProps = {
	version?: string;
	/** Avatar only, for the collapsed sidebar; otherwise avatar, name and email. */
	compact?: boolean;
};

/** The signed-in account at the bottom of the sidebar, and its menu. */
export const UserMenu = ({ version, compact = false }: UserMenuProps) => {
	const { data } = useSession();
	const router = useRouter();
	const [signingOut, setSigningOut] = useState(false);

	if (!data) return null;

	const { user } = data;
	const initial = (user.name || user.email || '?').charAt(0).toUpperCase();
	const fullName = user.name || user.email;
	// Title-case the role, e.g. "admin" → "Admin".
	const roleLabel = user.role
		? user.role.charAt(0).toUpperCase() + user.role.slice(1).toLowerCase()
		: null;

	const canManageTokens = userCan(
		{ id: user.id, role: user.role ?? null },
		{ apiToken: ['manage'] },
	);

	const handleSignOut = async () => {
		setSigningOut(true);
		await signOut();
		// Hard navigation so the root layout re-runs server-side without a
		// session and drops the app shell (NavRail / top bar).
		window.location.href = '/login';
	};

	return (
		<PopoverMenu
			header={
				// The fixed width is what gives a long email something to be
				// clipped against, since the menu is otherwise as wide as its
				// widest item.
				<div className="w-56 px-3 py-1 text-xs text-secondary dark:text-zinc-500">
					{version ? <p>Version {version}</p> : null}
					<Text text={fullName} variant={TextVariant.Strong} />
					{roleLabel ? <p className="text-[10px]">{roleLabel}</p> : null}
				</div>
			}
			items={[
				...(canManageTokens
					? [
							{
								label: 'API Tokens',
								onClick: () => router.push('/account/api-tokens'),
							},
						]
					: []),
				{
					label: signingOut ? 'Signing out…' : 'Sign out',
					onClick: () => {
						void handleSignOut();
					},
					disabled: signingOut,
				},
			]}
			footer={<ThemeSwitcher />}
			align={PopoverAlign.AboveLeft}
			className={compact ? undefined : 'w-full'}
			trigger={({ open, toggle }) =>
				compact ? (
					<button
						type="button"
						onClick={toggle}
						aria-haspopup="menu"
						aria-expanded={open}
						aria-label={`User menu (${fullName})`}
						title={fullName}
						className="flex h-10 w-10 items-center justify-center rounded-full border-2 border-white/90 bg-primary text-sm font-bold text-white transition-opacity hover:opacity-90 focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 focus-visible:outline-none dark:border-white/20"
					>
						{initial}
					</button>
				) : (
					<button
						type="button"
						onClick={toggle}
						aria-haspopup="menu"
						aria-expanded={open}
						aria-label="User menu"
						className="flex w-full items-center gap-2.5 rounded-2xl border border-white/80 bg-white/60 p-2 text-left transition-colors hover:bg-white/80 focus-visible:ring-2 focus-visible:ring-primary focus-visible:outline-none dark:border-white/10 dark:bg-white/[0.06] dark:hover:bg-white/[0.1]"
					>
						<span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-primary text-sm font-bold text-white">
							{initial}
						</span>
						<span className="flex min-w-0 flex-1 flex-col">
							<span className="truncate text-sm font-semibold text-heading dark:text-zinc-100">
								{fullName}
							</span>
							<span className="truncate text-xs text-secondary dark:text-zinc-400">
								{user.name ? user.email : roleLabel}
							</span>
						</span>
						<Icon
							name={IconName.ChevronRight}
							className={`h-4 w-4 shrink-0 text-secondary transition-transform dark:text-zinc-400 ${open ? 'rotate-90' : '-rotate-90'}`}
						/>
					</button>
				)
			}
		/>
	);
};
