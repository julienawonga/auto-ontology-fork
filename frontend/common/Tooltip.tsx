// SPDX-FileCopyrightText: Copyright (c) 2026, Julien Awonga.
// SPDX-License-Identifier: Apache-2.0

'use client';

import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';

type Delays = { openDelayDuration: number; skipDelayDuration: number };

const DelayContext = createContext<Delays>({ openDelayDuration: 400, skipDelayDuration: 0 });

// Shared timing for every tooltip under it. `skipDelayDuration` is accepted for
// call-site compatibility; once one tooltip is open the next one opens at once.
export const TooltipProvider = ({
	openDelayDuration = 400,
	skipDelayDuration = 0,
	children,
}: Partial<Delays> & { children: ReactNode }) => (
	<DelayContext.Provider value={{ openDelayDuration, skipDelayDuration }}>
		{children}
	</DelayContext.Provider>
);

type TooltipProps = {
	slotContent: ReactNode;
	side?: 'top' | 'bottom';
	align?: 'start' | 'center' | 'end';
	className?: string;
	children: ReactNode;
};

type Position = { top: number; left: number; transform: string };

const GAP = 6;

export const Tooltip = ({
	slotContent,
	side = 'bottom',
	align = 'center',
	className,
	children,
}: TooltipProps) => {
	const { openDelayDuration } = useContext(DelayContext);
	const [position, setPosition] = useState<Position | null>(null);
	const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

	const clear = () => {
		if (timer.current) clearTimeout(timer.current);
		timer.current = null;
	};

	const hide = useCallback(() => {
		clear();
		setPosition(null);
	}, []);

	const show = (target: HTMLElement) => {
		clear();
		timer.current = setTimeout(() => {
			// The wrapper has no box of its own; measure the element it wraps.
			const el = (target.firstElementChild as HTMLElement | null) ?? target;
			const rect = el.getBoundingClientRect();
			const x =
				align === 'start'
					? rect.left
					: align === 'end'
						? rect.right
						: rect.left + rect.width / 2;
			const tx = align === 'start' ? '0' : align === 'end' ? '-100%' : '-50%';
			setPosition(
				side === 'top'
					? { top: rect.top - GAP, left: x, transform: `translate(${tx}, -100%)` }
					: { top: rect.bottom + GAP, left: x, transform: `translate(${tx}, 0)` },
			);
		}, openDelayDuration);
	};

	useEffect(() => {
		if (!position) return;
		// A fixed tooltip would be left behind by scrolling, so close it.
		window.addEventListener('scroll', hide, true);
		return () => window.removeEventListener('scroll', hide, true);
	}, [position, hide]);

	useEffect(() => clear, []);

	return (
		<>
			<span
				className="contents"
				onMouseEnter={(e) => show(e.currentTarget)}
				onMouseLeave={hide}
				onFocus={(e) => show(e.currentTarget)}
				onBlur={hide}
				onKeyDown={(e) => e.key === 'Escape' && hide()}
			>
				{children}
			</span>
			{position &&
				createPortal(
					<div
						role="tooltip"
						className={className}
						style={{ position: 'fixed', pointerEvents: 'none', ...position }}
					>
						{slotContent}
					</div>,
					document.body,
				)}
		</>
	);
};
