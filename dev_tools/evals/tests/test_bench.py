# SPDX-License-Identifier: Apache-2.0

"""The bench, on synthetic cases, a fake backend (httpx MockTransport) and a fake scorer.

Nothing here talks to a real backend, a real model or a real database, and no
LiveSQLBench question, gold or knowledge entry is used.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from dev_tools.evals import __main__ as cli
from dev_tools.evals import bench
from dev_tools.evals.bench import (
    BenchError,
    NO_CHANGE,
    IMPROVED,
    REGRESSED,
    ScoredRun,
    compare_scored,
    decide,
    harness_score,
    load_run,
    resolve_workers,
    run_slice,
    score_run,
    split_cases,
    verify_slice,
)
from dev_tools.evals.lsb import Outcome, OfficialScore, mcnemar_exact
from dev_tools.evals.report import Report


def _write_cases(
    root: Path,
    dbs: dict[str, int],
    *,
    lsb: bool = False,
    tiers: tuple[str, ...] = ("simple", "moderate"),
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for db, n in dbs.items():
        cases = []
        for i in range(n):
            tier = tiers[i % len(tiers)]
            cases.append(
                {
                    "id": f"{db}.{i}",
                    "question": f"{db}.{i} question",
                    "tags": [f"tier:{tier.title()}" if lsb else tier],
                    "gold": [{"label": "g", "sql": "select 1", "expected": [[1]]}],
                }
            )
        (root / f"{db}.json").write_text(
            json.dumps({"database": db, "cases": cases}), encoding="utf-8"
        )
    return root


def _slice_ids(split: Path, name: str) -> set[str]:
    return {
        c["id"]
        for p in (split / name).glob("*.json")
        for c in json.loads(p.read_text(encoding="utf-8"))["cases"]
    }


# ------------------------------------------------------------------------ split


def test_split_is_deterministic_and_disjoint(tmp_path: Path) -> None:
    cases = _write_cases(tmp_path / "in", {"alpha": 20, "beta": 20})
    a = split_cases(cases, tmp_path / "a", seed=7, dev=0.3, canary=0.1)
    b = split_cases(cases, tmp_path / "b", seed=7, dev=0.3, canary=0.1)
    c = split_cases(cases, tmp_path / "c", seed=8, dev=0.3, canary=0.1)

    assert a == b and a["split_id"] == b["split_id"]
    assert _slice_ids(tmp_path / "a", "dev") == _slice_ids(tmp_path / "b", "dev")
    assert _slice_ids(tmp_path / "a", "dev") != _slice_ids(tmp_path / "c", "dev")
    ids = [_slice_ids(tmp_path / "a", s) for s in ("dev", "heldout", "canary")]
    assert not (ids[0] & ids[1] or ids[0] & ids[2] or ids[1] & ids[2])
    assert set().union(*ids) == {
        f"{d}.{i}" for d in ("alpha", "beta") for i in range(20)
    }
    assert c["counts"] == a["counts"]


def test_split_is_stratified_by_database_and_tier(tmp_path: Path) -> None:
    cases = _write_cases(tmp_path / "in", {"alpha": 40, "beta": 40})
    split_cases(cases, tmp_path / "s", seed=1, dev=0.3, canary=0.1)
    for db in ("alpha", "beta"):
        for tier in ("simple", "moderate"):  # 20 per stratum
            counts = {}
            for name in ("dev", "heldout", "canary"):
                path = tmp_path / "s" / name / f"{db}.json"
                raw = json.loads(path.read_text(encoding="utf-8"))
                counts[name] = sum(tier in c["tags"] for c in raw["cases"])
            assert counts == {"dev": 6, "heldout": 12, "canary": 2}


def test_split_freezes_every_slice_and_run_detects_tampering(tmp_path: Path) -> None:
    cases = _write_cases(tmp_path / "in", {"alpha": 10})
    split_cases(cases, tmp_path / "s", seed=1, dev=0.3, canary=0.1)
    listing = (tmp_path / "s" / "heldout" / "FROZEN.sha256").read_text(encoding="utf-8")
    assert listing.strip().endswith("alpha.json")
    assert len(listing.split()[0]) == 64
    assert verify_slice(tmp_path / "s", "heldout")

    path = tmp_path / "s" / "heldout" / "alpha.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["cases"][0]["question"] = "edited"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(BenchError, match="not what was frozen"):
        verify_slice(tmp_path / "s", "heldout")


def test_split_refuses_to_overwrite_and_bad_ratios(tmp_path: Path) -> None:
    cases = _write_cases(tmp_path / "in", {"alpha": 10})
    split_cases(cases, tmp_path / "s", seed=1, dev=0.3, canary=0.1)
    with pytest.raises(BenchError, match="refusing to overwrite"):
        split_cases(cases, tmp_path / "s", seed=2, dev=0.3, canary=0.1)
    with pytest.raises(BenchError, match="dev"):
        split_cases(cases, tmp_path / "t", seed=1, dev=0.8, canary=0.3)


def test_split_refuses_lsb_cases_inside_the_repo(tmp_path: Path) -> None:
    cases = _write_cases(tmp_path / "in", {"alpha": 10}, lsb=True, tiers=("simple",))
    out = bench.REPO_ROOT / "never-written-bench-split"
    with pytest.raises(BenchError, match="inside this repository"):
        split_cases(cases, out, seed=1, dev=0.3, canary=0.1)
    assert not out.exists()


# ---------------------------------------------------------------------- workers


@pytest.mark.parametrize(
    ("requested", "cap", "expected"),
    [
        ("auto", 4, 4),
        ("auto", 12, 6),
        ("8", 4, 4),
        ("2", 4, 2),
        ("9", None, 6),
        ("auto", None, 2),
    ],
)
def test_resolve_workers_respects_the_backend_cap(
    requested: str, cap: int | None, expected: int
) -> None:
    assert resolve_workers(requested, cap)[0] == expected


def test_resolve_workers_rejects_nonsense() -> None:
    for bad in ("0", "many"):
        with pytest.raises(BenchError):
            resolve_workers(bad, 4)


# ---------------------------------------------------------------- fake backend


class FakeBackend:
    """Health, index status and chat, with scripted infrastructure failures."""

    def __init__(
        self,
        *,
        cap: int | None = 2,
        wrong: set[str] = frozenset(),  # type: ignore[assignment]
        flaky: set[str] = frozenset(),
        always_down: set[str] = frozenset(),  # type: ignore[assignment]
        dbs: tuple[str, ...] = ("alpha", "beta"),
        git_sha: str | None = None,
    ) -> None:
        self.cap, self.wrong, self.flaky, self.always_down = (
            cap,
            wrong,
            flaky,
            always_down,
        )
        self.dbs, self.git_sha = dbs, git_sha
        self.calls: dict[str, int] = {}
        self.in_flight = self.max_in_flight = 0
        self.headers: list[httpx.Headers] = []
        self.lock = threading.Lock()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/health":
            body: dict[str, Any] = {"status": "ok"}
            if self.cap is not None:
                body["chat_workers"] = {"cap": self.cap, "active": 0}
            if self.git_sha:
                body["git_sha"] = self.git_sha
            return httpx.Response(200, json=body)
        if path == "/api/semantic-compilation/status":
            index = [
                {"database_name": d, "expected": 5, "indexed": 5, "missing": 0}
                for d in self.dbs
            ]
            return httpx.Response(200, json={"index": index})
        question = json.loads(request.content)["question"]
        cid = question.removesuffix(" question")
        with self.lock:
            self.headers.append(request.headers)
            self.calls[cid] = self.calls.get(cid, 0) + 1
            first = self.calls[cid] == 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(0.01)
            if cid in self.always_down or (cid in self.flaky and first):
                return httpx.Response(500, text="pool exhausted")
            value = 2 if cid in self.wrong else 1
            event = {
                "type": "result",
                "answer": {
                    "sql_code": "select 1",
                    "sql_response_from_db": [{"n": value}],
                },
            }
            return httpx.Response(
                200, text=f"data: {json.dumps(event)}\ndata: [DONE]\n"
            )
        finally:
            with self.lock:
                self.in_flight -= 1


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> Any:
    real_client = httpx.Client
    holder: dict[str, FakeBackend] = {}

    def install(fake: FakeBackend) -> FakeBackend:
        holder["b"] = fake
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **kw: real_client(transport=httpx.MockTransport(fake.handler), **kw),
        )
        return fake

    return install


@pytest.fixture
def split(tmp_path: Path) -> Path:
    cases = _write_cases(tmp_path / "in", {"alpha": 16, "beta": 16})
    split_cases(cases, tmp_path / "split", seed=3, dev=0.25, canary=0.0)
    return tmp_path / "split"


def _run(
    split: Path, tmp_path: Path, label: str, slice_name: str = "heldout", **kw: Any
) -> Path:
    return run_slice(
        split, slice_name, label=label, url="http://backend:3001",
        out_dir=tmp_path / "runs", **kw,
    )  # fmt: skip


def test_run_caps_workers_by_the_health_cap_in_one_process(
    backend: Any, split: Path, tmp_path: Path
) -> None:
    fake = backend(FakeBackend(cap=2))
    run_dir = _run(
        split, tmp_path, "main", workers="auto", repeats=2, token="secret-token"
    )

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["workers"] == {"requested": "auto", "backend_cap": 2, "used": 2}
    assert 1 <= fake.max_in_flight <= 2
    assert {h["x-internal-token"] for h in fake.headers} == {"secret-token"}
    assert "secret-token" not in json.dumps(manifest)
    # every database of the slice, every repeat, in one run directory
    heldout = _slice_ids(split, "heldout")
    assert manifest["totals"]["runs"] == 2 * len(heldout)
    assert set(manifest["databases"]) == {"alpha", "beta"}
    assert (run_dir / "merged.json").is_file()
    assert Report.load(run_dir / "reports" / "alpha.replayed.json").meta["workers"] == 2


def test_manifest_records_label_sha_times_notes_and_replay_rule(
    backend: Any, split: Path, tmp_path: Path
) -> None:
    backend(FakeBackend(git_sha="abc1234"))
    run_dir = _run(split, tmp_path, "cand", notes=["TOP_K=8", "reranker=off"])
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["label"] == "cand" and manifest["slice"] == "heldout"
    assert manifest["git_sha"] == "abc1234" and manifest["git_sha_source"] == "endpoint"
    assert manifest["notes"] == ["TOP_K=8", "reranker=off"]
    assert manifest["started"] <= manifest["ended"]
    assert (
        manifest["split_id"]
        == json.loads((split / "split.json").read_text())["split_id"]
    )
    assert manifest["replay"]["rule"] == "errored, no SQL, under 5s"
    assert manifest["is_evidence"] is True
    assert all(len(d["cases_sha256"]) == 64 for d in manifest["databases"].values())

    backend(FakeBackend())  # no sha on the endpoint: user-supplied, else unknown
    given = json.loads(
        (_run(split, tmp_path, "u1", git_sha="deadbee") / "manifest.json").read_text()
    )
    assert (given["git_sha"], given["git_sha_source"]) == ("deadbee", "user")
    unknown = json.loads((_run(split, tmp_path, "u2") / "manifest.json").read_text())
    assert (unknown["git_sha"], unknown["git_sha_source"]) == (None, "unknown")
    assert _run_dev_flag(split, tmp_path) is False


def _run_dev_flag(split: Path, tmp_path: Path) -> bool:
    run_dir = _run(split, tmp_path, "devrun", slice_name="dev")
    return json.loads((run_dir / "manifest.json").read_text())["is_evidence"]


def test_only_infrastructure_failures_are_replayed(
    backend: Any, split: Path, tmp_path: Path
) -> None:
    heldout = sorted(_slice_ids(split, "heldout"))
    flaky, wrong, down = {heldout[0]}, {heldout[1]}, {heldout[2]}
    fake = backend(FakeBackend(flaky=flaky, wrong=wrong, always_down=down))
    run_dir = _run(split, tmp_path, "main")
    manifest = json.loads((run_dir / "manifest.json").read_text())

    assert fake.calls[heldout[0]] == 2  # failed instantly once, replayed, now fine
    assert fake.calls[heldout[1]] == 1  # a wrong answer is the engine's: not replayed
    assert fake.calls[heldout[2]] == 2  # replayed once only, still down
    assert fake.calls[heldout[3]] == 1
    assert manifest["replay"]["infra_failures_before"] == 2
    assert manifest["replay"]["infra_failures_after"] == 1
    assert manifest["replay"]["passes"] == 1
    replayed = {
        cid: res
        for db in ("alpha", "beta")
        for cid, res in Report.load(
            run_dir / "reports" / f"{db}.replayed.json"
        ).cases.items()
    }
    assert replayed[heldout[0]].runs[0].passed
    originals = Report.load(run_dir / "reports" / f"{heldout[0].split('.')[0]}.json")
    assert (
        originals.cases[heldout[0]].runs[0].error is not None
    )  # the raw report is kept


def test_run_refuses_a_slice_that_no_longer_matches_its_freeze(
    backend: Any, split: Path, tmp_path: Path
) -> None:
    fake = backend(FakeBackend())
    path = next((split / "heldout").glob("*.json"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["cases"][0]["gold"][0]["expected"] = [[99]]
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(BenchError, match="not what was frozen"):
        _run(split, tmp_path, "main")
    assert fake.calls == {}


# ------------------------------------------------------------- verdict / scoring


def test_decide() -> None:
    assert decide(0.03, 0, 6) == IMPROVED
    assert decide(0.03, 6, 0) == REGRESSED
    assert decide(0.07, 1, 7) == NO_CHANGE
    assert decide(1.0, 0, 0) == NO_CHANGE
    assert decide(0.05, 0, 5) == NO_CHANGE  # alpha is strict


def _scored(
    tmp_path: Path,
    label: str,
    passes: list[list[bool]],
    *,
    slice_name: str = "heldout",
    split_id: str = "s1",
    infra: set[str] = frozenset(),  # type: ignore[assignment]
) -> ScoredRun:
    """A scored run from per-question lists of per-repeat outcomes (fake scorer)."""
    outcomes = {
        f"q{i}": [Outcome(p, "x") for p in runs] for i, runs in enumerate(passes)
    }
    score = OfficialScore(label, outcomes, {c: "simple" for c in outcomes})
    manifest = {"label": label, "slice": slice_name, "split_id": split_id,
                "repeats": len(passes[0]), "replay": {}}  # fmt: skip
    data = bench.RunData(tmp_path / label, manifest, {}, {})
    return ScoredRun(data, score, set(infra))


def test_verdict_wires_mcnemar_and_the_direction(tmp_path: Path) -> None:
    base = _scored(tmp_path, "base", [[True]] * 4 + [[False]] * 9 + [[True]] * 7)
    cand = _scored(tmp_path, "cand", [[False]] * 4 + [[True]] * 9 + [[True]] * 7)
    v = compare_scored(base, cand)
    assert (v.only_base, v.only_cand, v.both, v.neither) == (4, 9, 7, 0)
    assert v.p_value == pytest.approx(mcnemar_exact(4, 9))
    assert v.verdict == NO_CHANGE  # p = 0.267: a 5-question gain is inside the noise

    cand2 = _scored(tmp_path, "cand2", [[True]] * 4 + [[True]] * 9 + [[True]] * 7)
    assert compare_scored(base, cand2).verdict == IMPROVED
    assert compare_scored(cand2, base).verdict == REGRESSED


def test_verdict_notes_that_dev_is_not_evidence(tmp_path: Path) -> None:
    base = _scored(tmp_path, "b", [[False]] * 8, slice_name="dev")
    cand = _scored(tmp_path, "c", [[True]] * 8, slice_name="dev")
    v = compare_scored(base, cand)
    assert v.verdict == IMPROVED
    assert any("NOT evidence" in n for n in v.notes)
    assert any("heldout slice" in n for n in v.notes)

    held = compare_scored(
        _scored(tmp_path, "b", [[False]] * 8), _scored(tmp_path, "c", [[True]] * 8)
    )
    assert not any("NOT evidence" in n for n in held.notes)


def test_runs_from_different_splits_or_slices_are_refused(tmp_path: Path) -> None:
    a = _scored(tmp_path, "a", [[True]] * 3)
    with pytest.raises(BenchError, match="different split"):
        compare_scored(a, _scored(tmp_path, "b", [[True]] * 3, split_id="s2"))
    with pytest.raises(BenchError, match="different slice"):
        compare_scored(a, _scored(tmp_path, "b", [[True]] * 3, slice_name="dev"))


def test_noise_band_from_repeats_and_from_a_sibling_run(tmp_path: Path) -> None:
    # baseline repeats: q0 flips between its two runs, q1 and q2 are stable
    base = _scored(tmp_path, "base", [[True, False], [True, True], [False, False]])
    cand = _scored(tmp_path, "cand", [[True, True]] * 3)
    v = compare_scored(base, cand)
    assert v.noise_rate == pytest.approx(1 / 3)
    assert "repeats" in v.noise_source

    single = _scored(tmp_path, "base", [[True], [True], [False]])
    assert (
        compare_scored(single, _scored(tmp_path, "c", [[True]] * 3)).noise_rate is None
    )
    sibling = _scored(tmp_path, "sib", [[False], [True], [False]])
    v2 = compare_scored(single, _scored(tmp_path, "c", [[True]] * 3), sibling)
    assert v2.noise_rate == pytest.approx(1 / 3) and "sibling" in v2.noise_source
    text = bench.render_verdict(single, single, v2)
    assert "noise band" in text and "VERDICT" in text


def test_unreplayed_infrastructure_failures_are_excluded_for_both_arms(
    tmp_path: Path,
) -> None:
    base = _scored(tmp_path, "b", [[True]] * 6 + [[False]], infra={"q6"})
    cand = _scored(tmp_path, "c", [[False]] * 6 + [[True]], infra={"q0"})
    v = compare_scored(base, cand)
    assert v.excluded == 2 and v.paired == 5
    assert any("excluded" in n for n in v.notes)


# ---------------------------------------------------- end to end, harness scoring


def test_score_cli_on_two_runs_with_the_harness_fallback(
    backend: Any, split: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    heldout = sorted(_slice_ids(split, "heldout"))
    backend(FakeBackend())
    base_dir = _run(split, tmp_path, "base")
    backend(FakeBackend(wrong=set(heldout[:8])))
    cand_dir = _run(split, tmp_path, "cand")

    code = cli.main(
        ["bench", "score", "--run", str(cand_dir), "--baseline", str(base_dir)]
    )
    out = capsys.readouterr().out

    assert code == 1  # regressed: the candidate lost 8 questions, p = 0.0078
    assert "harness comparison" in out
    assert "only baseline 8   only candidate 0" in out
    assert "VERDICT: REGRESSED" in out
    assert "Paraphrased" in out

    assert (
        cli.main(
            ["bench", "score", "--run", str(base_dir), "--baseline", str(cand_dir)]
        )
        == 0
    )
    assert "VERDICT: IMPROVED" in capsys.readouterr().out


def test_score_uses_the_official_scorer_for_lsb_splits(
    monkeypatch: pytest.MonkeyPatch, backend: Any, tmp_path: Path
) -> None:
    cases = _write_cases(tmp_path / "in", {"alpha": 12}, lsb=True)
    split_cases(cases, tmp_path / "split", seed=1, dev=0.3, canary=0.0)
    backend(FakeBackend(dbs=("alpha",)))
    run_dir = _run(tmp_path / "split", tmp_path, "main")
    seen: list[str] = []

    def fake_official(
        report: Report, cases: Any, executor: Any, **kw: Any
    ) -> OfficialScore:
        seen.append(kw["mode"])
        return harness_score(report, cases)

    monkeypatch.setattr(bench, "score_report", fake_official)
    monkeypatch.setattr(
        bench,
        "PostgresExecutor",
        lambda *a, **k: type("E", (), {"close": lambda s: None})(),
    )

    scorer, close = bench.make_scorer(True, "postgresql://u:p@h/{db}", "official", 5.0)
    scored = score_run(load_run(run_dir), scorer)
    close()
    assert seen == ["official"] and scored.score.tier_scores()["Overall"][0] == len(
        _slice_ids(tmp_path / "split", "heldout")
    )
    with pytest.raises(BenchError, match="dsn-template"):
        bench.make_scorer(True, None, "official", 5.0)
