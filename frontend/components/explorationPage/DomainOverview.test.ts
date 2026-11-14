// SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES.
// All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
	calculateCertifiedPercentage,
	clampFraction,
	domainBubbleSize,
	domainLinkDistance,
	domainLinkWidth,
	formatCertifiedText,
	formatLinkLabel,
	getTopNeighbours,
	initialDomainPositions,
} from './DomainOverview';
import type { SemanticDomain, SemanticDomainLink } from '@/types/exploration';

describe('domain overview pure helpers', () => {
	it('clamps bubble sizes and grows with term count', () => {
		expect(domainBubbleSize(0)).toBe(72);
		expect(domainBubbleSize(100)).toBeGreaterThan(domainBubbleSize(1));
		expect(domainBubbleSize(10000)).toBe(220);
	});

	it('maps link weights to widths from one to eight pixels', () => {
		expect(domainLinkWidth(1, 1)).toBe(1);
		expect(domainLinkWidth(1, 10)).toBe(1);
		expect(domainLinkWidth(10, 10)).toBe(8);
	});

	it('formats link labels accurately', () => {
		expect(formatLinkLabel(5, 3)).toBe('5 links · 3 tables');
		expect(formatLinkLabel(1, 0)).toBe('1 links · 0 tables');
	});

	it('formats certified term text and percentages correctly', () => {
		expect(formatCertifiedText(3, 10)).toBe('3 of 10 certified');
		expect(calculateCertifiedPercentage(3, 10)).toBe(30);
		expect(calculateCertifiedPercentage(0, 0)).toBe(0);
		expect(calculateCertifiedPercentage(5, 3)).toBe(167);
	});

	it('computes top neighbours sorted by link weight', () => {
		const domains: SemanticDomain[] = [
			{ id: 'd1', name: 'Finance', term_ids: ['t1'], term_count: 1 },
			{ id: 'd2', name: 'Sales', term_ids: ['t2'], term_count: 1 },
			{ id: 'd3', name: 'Marketing', term_ids: ['t3'], term_count: 1 },
			{ id: 'd4', name: 'HR', term_ids: ['t4'], term_count: 1 },
		];
		const links: SemanticDomainLink[] = [
			{ source: 'd1', target: 'd2', weight: 10 },
			{ source: 'd1', target: 'd3', weight: 25 },
			{ source: 'd4', target: 'd1', weight: 5 },
		];

		const neighbours = getTopNeighbours('d1', domains, links, 2);
		expect(neighbours).toHaveLength(2);
		expect(neighbours[0].domain.id).toBe('d3');
		expect(neighbours[0].weight).toBe(25);
		expect(neighbours[1].domain.id).toBe('d2');
		expect(neighbours[1].weight).toBe(10);
	});
});

describe('domain layout helpers', () => {
	it('puts two domains side by side and keeps every position inside the canvas', () => {
		const two = initialDomainPositions(['a', 'b']);
		expect(Math.abs(two.a.y - two.b.y)).toBeLessThan(1e-9);
		expect(two.a.x).not.toBe(two.b.x);
		const many = initialDomainPositions(['a', 'b', 'c', 'd', 'e', 'f']);
		for (const point of Object.values(many)) {
			expect(point.x).toBeGreaterThan(0);
			expect(point.x).toBeLessThan(1);
			expect(point.y).toBeGreaterThan(0);
			expect(point.y).toBeLessThan(1);
		}
	});

	it('keeps a dragged bubble inside the canvas', () => {
		expect(clampFraction(-3)).toBe(0.08);
		expect(clampFraction(5)).toBe(0.92);
		expect(clampFraction(0.5)).toBe(0.5);
	});
});

describe('domain link distance', () => {
	it('pulls strongly linked domains closer together', () => {
		expect(domainLinkDistance(100, 100)).toBeLessThan(domainLinkDistance(10, 100));
		expect(domainLinkDistance(1, 1)).toBe(240);
		expect(domainLinkDistance(5, 0)).toBe(440);
	});
});
